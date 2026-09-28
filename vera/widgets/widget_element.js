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
                                                          before it has read anything — draw() and the element use it, marked.
                                                          The sample is for a record with NO source or one never read; a read
                                                          that answered empty draws the form's own empty state, a read that
                                                          failed the last good reading dimmed — never the sample
     window.VeraWidgetConfig.open(opts)                → the WidgetConfig board as a sheet (catalogue · record · live preview);
                                                          Promise<record | null> — every picker and every ⚙ goes through it
     <vera-widget record='{…json…}' size="m|auto" base="">   .record (property) · .refresh() · .read()
       a COMPOSITE record reads its children's own sources (each child a record; a child whose source is $subject.<path>
       takes that slice of the composite's one read); frame.motion false holds a moving form still; skin sets the
       pack (data-style) on the element so the page's own pack rules dress it
       events (bubbling, composed): widget:open · widget:pin · widget:ask · widget:ops · widget:print · widget:mute ·
                                    widget:refresh · widget:resize · widget:rendered

   Dependency-free, theme variables with fallbacks. The chat's reply block, the canvas item and the dashboard
   tile all draw through here — the chat's own nine renderers became these, and the three boards' forms are drawn
   here in their own right (the vb- section): the Widgets board's still forms, the WidgetsMotion board's moving and
   isometric forms, the WidgetsIso board's objects and frames — the iso ones on window.VeraISO (/ui/iso.js, loaded by
   the element itself), each with a sample face. A record's projection picks the iso drawing of a form that has both. */
(function () {
  'use strict';
  if (window.VeraWidget) return;
  /* THE TEXT-SIZE SETTING (the widget review, round 2). vera-ui.js paints html[data-text] = compact | default | large |
     larger and floors the small px font sizes it can reach; what it cannot reach - a size inside a clamp(), a style this
     file builds, an SVG attribute - went on drawing at 6.5-9.5 px. Every font size under 13 px in this file's CSS is
     written through fontScale(): calc(max(floor, size) * factor), the floor and the factor two variables the setting
     sets on the document (--vw-fmin 0 / 10 / 11 / 12 px, --vw-fx 1 / 1 / 1.1 / 1.22), so every face - in a shadow
     root or on the page - follows the setting and nothing is drawn below 10 px by default. */
  function fontScale(css) {
    const f = (n) => 'calc(max(var(--vw-fmin, 10px), ' + n + 'px) * var(--vw-fx, 1))';
    return String(css).replace(/font-size:\s*([0-9.]+)px/g, (m, n) => (+n >= 13 ? m : 'font-size:' + f(n)))
      .replace(/font-size:\s*clamp\(([0-9.]+)px,/g, (m, n) => 'font-size:clamp(' + f(n) + ',');
  }
  const TEXT_SCALE_CSS = 'html{--vw-fmin:10px;--vw-fx:1}html[data-text="compact"]{--vw-fmin:0px;--vw-fx:1}html[data-text="large"]{--vw-fmin:11px;--vw-fx:1.1}html[data-text="larger"]{--vw-fmin:12px;--vw-fx:1.22}'
    /* a dashboard is read from across the room (owner, 2026-09-27: "the font on lots of widgets is still too small like the
       warnings and live events widgets it should be larger on the dashboards"): a higher floor there, at each setting */
    + '.vw-vgraph-host.min .vg-bottom-area{display:none!important}'
    + '.vw-vgraph-host .vw-vgkey{position:absolute;right:18px;bottom:16px;z-index:6;display:flex;flex-wrap:wrap;justify-content:flex-end;gap:4px 10px;max-width:62%;font:10.5px var(--f-ui,var(--sans,system-ui,sans-serif));color:var(--t2,var(--dim2,#8a92a0));pointer-events:none}.vw-vgraph-host .vw-vgkey span{display:inline-flex;align-items:center;gap:5px;padding:1px 6px;border-radius:999px;background:color-mix(in srgb,var(--bg1,#15171c) 80%,transparent)}.vw-vgraph-host .vw-vgkey i{width:8px;height:8px;border-radius:2px}'
    + '.dash-grid vera-widget{--vw-fmin:11.5px}html[data-text="compact"] .dash-grid vera-widget{--vw-fmin:9px}html[data-text="large"] .dash-grid vera-widget{--vw-fmin:12.5px}html[data-text="larger"] .dash-grid vera-widget{--vw-fmin:13.5px}';
  try { if (typeof document !== 'undefined' && document.head && !document.getElementById('vw-text-scale')) { const st = document.createElement('style'); st.id = 'vw-text-scale'; st.textContent = TEXT_SCALE_CSS; document.head.appendChild(st); } } catch (_) {}
  const textKOf = (el) => { try { const cs = getComputedStyle(el); const fmin = parseFloat(cs.getPropertyValue('--vw-fmin')), fx = parseFloat(cs.getPropertyValue('--vw-fx')) || 1; return Math.max(1, Math.max(isFinite(fmin) ? fmin : 10, 9.5) * fx / 9.5); } catch (_) { return 1; } };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const num = (v) => { const n = typeof v === 'number' ? v : parseFloat(v); return isFinite(n) ? n : 0; };
  const fmt = (v) => { const n = num(v); return Math.abs(n) >= 100 ? Math.round(n).toLocaleString() : (Math.round(n * 10) / 10).toString(); };
  const SIZES = ['xs', 's', 'm', 'l', 'xl'];
  const HEIGHT = { xs: 14, s: 24, m: 96, l: 130, xl: 220 };
  const EMPTY = (t) => '<span class="wempty">' + esc(t) + '</span>';
  /* A source that was READ and answered with nothing, said once for every form (Notes/42 defect 71a). The forms' own
     empty lines - "a counter needs a value", "no numbers to draw" - explain a record the form cannot draw, which is a
     different thing; used for an empty read they made the tile look as though its values had been cleared. A real
     zero never comes here: zero is a reading. */
  const NODATA = (src, size) => (size === 'xs' || size === 's')
    ? '<span class="vw-xs vw-nodata">no data</span>'
    : '<div class="vw-nodata"><b>no data</b><i>' + esc(src ? src + ' read \u00b7 nothing to show' : 'nothing read') + '</i></div>';
  // a form id the catalogue names but this file does not draw in its own right yet resolves to the nearest face; the
  // board's captions too (the motion and iso forms land in the next slices)
  // a form id the catalogue names but this file does not draw in its own right resolves to the nearest face; the board's
  // captions and the WidgetConfig board's own ids too
  const ALIAS = { sparkline: 'trace', line: 'trace', chart: 'trace', parts: 'donut', tree: 'files', stages: 'stepper', program: 'stepper', ask: 'string', form: 'kv', rate: 'counter',
                  iso: 'table', controls: 'pills', button: 'string', header: 'string', galaxy: 'context_graph', battery: 'level', tablei: 'table', checks: 'checklist',
                  memgraph: 'minigraph', logi: 'log', notice: 'announcement', trend: 'hero', sparks: 'small-multiples', multiline: 'lines', 'multi-line': 'lines' };
  // what this file draws today (the rest of the catalogue resolves through ALIAS or says so)
  const DRAWN = { trace: 'series', radial: 'level', counter: 'level', bar: 'level', bars: 'values', thermo: 'values', heat: 'matrix', matrix: 'matrix', donut: 'parts',
                  stack: 'items', pills: 'values', log: 'events', lane: 'events', table: 'items', files: 'items', list: 'items', checklist: 'items', stepper: 'stages', globe: 'points',
                  calendar: 'calendar', string: 'string', kv: 'values', pipes: 'graph', context_graph: 'graph', structgraph: 'graph', scatter: 'points', panel: 'panel', composite: 'composite',
                  // the Widgets board's still forms, drawn in their own right
                  feed: 'events', gallery: 'items', terminal: 'string', agenda: 'calendar', people: 'items', links: 'items', announcement: 'string', board: 'items', hero: 'level', gauge: 'level', carousel: 'items',
                  'small-multiples': 'series', ring: 'level', area: 'series', histogram: 'values', waterfall: 'values', treemap: 'parts', radar: 'values', gantt: 'stages', flow: 'graph', bullet: 'values',
                  'stacked-bar': 'parts', threshold: 'values', column: 'values', graph: 'graph', numbers: 'values', minigraph: 'graph', ranked: 'values', meter: 'level', funnel: 'stages', waffle: 'parts',
                  lollipop: 'values', box: 'values', slope: 'series', horizon: 'series', diverging: 'values', step: 'series', level: 'level', candles: 'ohlcv', 'spark-table': 'items', rings: 'values',
                  timeline: 'events', bump: 'series', dots: 'matrix', pareto: 'values', tabs: 'matrix', slider: 'values', node: 'values', glance: 'values', pipeline: 'stages', compare: 'values', rail: 'items',
                  // the WidgetsMotion and WidgetsIso boards' forms (the iso ones draw on VeraISO)
                  dial: 'level', tank: 'level', turbine: 'rate', ticker: 'rate', scope: 'series', pulse: 'events', comet: 'events', sweep: 'events', activity: 'events', notices: 'events', 'split-flap': 'string', frame: 'string',
                  orbit: 'items', shelf: 'items', racks: 'items', stacks: 'parts', 'meter-panel': 'values', conveyor: 'stages', approvals: 'stages', city: 'graph', topology: 'graph', diagram: 'graph',
                  library: 'items', pages: 'items', wiki: 'items', devices: 'items', notebook: 'items', hosts: 'items', containers: 'items', models: 'items', datasets: 'items', sandboxes: 'items',
                  // the table family (defect 50)
                  rows: 'items', cards: 'items', temps: 'values',
                  // the capability-output forms (the widget review, round 2)
                  json: 'values', diff: 'string', code: 'string', progress: 'stages', status: 'values', media: 'string', error: 'string', markdown: 'string',
                  // the calendar forms and the Vera graph form (the widget review, round 3)
                  month: 'calendar', schedule: 'calendar', calnav: 'calendar', vgraph: 'graph',
                  /* the multi-line chart (2026-09-28) */
                  lines: 'series' };
  const canon = (form) => { const f = String(form || '').toLowerCase(); return DRAWN[f] ? f : (ALIAS[f] || f); };
  // the Loop Lab's pictures (see the CI section): each draws the ci payload whole, at every size
  const CI_FORMS = /^(status-matrix|race-green|test-grid|ci-board|run-track|run-compare|ci-pulse|ci-fleet|ci-run|census-commits|element|loop-perf|census-live|census-timeline)$/;

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
    if (CI_FORMS.test(form)) return x;   // a CI picture reads its payload whole
    if (/^(json|diff|code|progress|status|media|error|markdown|month|schedule|calnav|vgraph)$/.test(form)) return x;   // a result form reads the answer whole
    if (Array.isArray(x)) return x;
    if (typeof x === 'object') {
      if (/^(radial|counter|bar|hero|meter|level|ring|gauge|dial|tank)$/.test(form) && typeof x.value === 'number') return x;   // a level with its trend beside it is the level, not its trend
      if ((form === 'pipes' || form === 'context_graph') && Array.isArray(x.nodes)) return x;
      if (form === 'globe' && ['points', 'pins', 'hosts', 'events', 'nodes', 'items', 'rows'].some((k) => Array.isArray(x[k]))) return x;   // the globe reads its record whole: the points AND the links, the night, the page size
      if (form === 'stepper' && (Array.isArray(x.stages) || Array.isArray(x.steps))) return x;
      const ks = Object.keys(x); if ((form === 'thermo' || form === 'heat' || form === 'bars' || form === 'donut' || form === 'pills' || form === 'kv' || form === 'stack') && ks.length >= 2 && ks.every((k) => typeof x[k] === 'number')) return x;
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) if (x[k] != null) { const f = formByShape(x[k], depth + 1); if (f) return dataFor(x[k], form, depth + 1); }
      for (const k of ['data', 'result', 'items', 'rows', 'points', 'series', 'history', 'values', 'entries', 'results']) if (Array.isArray(x[k]) && !x[k].length) return x[k];   // an empty read is empty, not an envelope to list
      if (/^(items|events|stages)$/.test(DRAWN[form] || '')) { const la = ks.filter((k) => Array.isArray(x[k])); if (la.length === 1) return dataFor(x[la[0]], form, depth + 1); }   // a list form reads the answer's one list, whatever it is called ({todos, count})
    }
    return x;
  }
  const series = (d) => (Array.isArray(d) ? d : []).map((p) => typeof p === 'number' ? p : num(p && (p.v ?? p.value ?? p.y ?? p.close)));
  const keyed = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? Object.keys(d).filter((k) => typeof d[k] === 'number').map((k) => [k, d[k]]) : (Array.isArray(d) ? d.filter((r) => r && typeof r === 'object' && ['v', 'value', 'n', 'count'].some((k) => typeof r[k] === 'number')).map((r) => [String(r.name ?? r.k ?? r.key ?? r.label ?? ''), num(r.v ?? r.value ?? r.n ?? r.count)]).filter((kv) => kv[0]) : []);
  const rows = (d) => (Array.isArray(d) ? d : []).filter((r) => r && typeof r === 'object');
  const level = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? { v: num(d.value), lo: num(d.min ?? 0), hi: num(d.max ?? 100), unit: typeof d.unit === 'string' ? d.unit : '', delta: d.delta, bounded: d.max != null || d.min != null } : (typeof d === 'number' ? { v: d, lo: 0, hi: 100, unit: '', bounded: false } : null);
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
  const MAP_WORDS = new Set(['pick', 'keys', 'reverse', 'entries', 'count', 'sum', 'split', 'fields', 'of', 'total', 'where', 'span']);   // a map's own words, never a field rename
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
    // pick: {label: path, ...} builds the thing from paths anywhere in the answer (a fleet from proxmox.running, docker.running
    // and ollama.online) - keys: [name, ...] keeps only those entries of a dict, or those rows, in that order
    let picked = false;
    if (map.pick && typeof map.pick === 'object' && !Array.isArray(map.pick)) { const o = {}; Object.keys(map.pick).forEach((k) => { const v = pick(x, map.pick[k]); if (v !== undefined) o[k] = v; });
      if (Object.keys(o).length) { x = (cont === 'rows' || cont === 'events' || cont === 'points') ? Object.keys(o).map((k) => ({ name: k, value: o[k] })) : o; picked = true; } }
    const only = Array.isArray(map.keys) ? map.keys.map(String) : null, nm = (r) => String(r.name ?? r.id ?? r.label ?? r.key ?? '');
    const keep = (b) => { if (!only) return b; if (Array.isArray(b)) return only.map((k) => b.find((r) => r && typeof r === 'object' && nm(r) === k)).filter(Boolean); if (b && typeof b === 'object') { const o = {}; only.forEach((k) => { if (b[k] !== undefined) o[k] = b[k]; }); return o; } return b; };
    if (sh === 'level' || sh === 'rate') { const o = (x && typeof x === 'object' && !Array.isArray(x)) ? Object.assign({}, x) : { value: x }; let hit = false;
      /* of + total: one figure made from a list in the answer (or a dict of things - workers keyed by id): total: true
         counts its rows, total: '<path>' adds that field up; where: {field: value | [values]} keeps only the rows that
         match first; max: true is the whole list's count (busy workers OF all of them) */
      if (map.of != null && map.total != null) { let L = pick(x, map.of); if (L && typeof L === 'object' && !Array.isArray(L)) L = Object.values(L);
        if (Array.isArray(L)) { const all = L.filter((r) => r && typeof r === 'object'), w = (map.where && typeof map.where === 'object') ? map.where : null;
          const like = (want, got) => { const s = String(want); return s.endsWith('*') ? got.startsWith(s.slice(0, -1)) : s === got; };   // 'running*' is any status that begins so
          const sel = w ? all.filter((r) => Object.keys(w).every((k) => { const got = String(pick(r, k)); return (Array.isArray(w[k]) ? w[k] : [w[k]]).some((x) => like(x, got)); })) : all;
          o.value = map.total === true ? sel.length : sel.reduce((s, r) => s + num(pick(r, map.total)), 0); if (map.max === true) o.max = all.length; hit = true; } }
      ['value', 'rate', 'min', 'max', 'unit', 'delta', 'trend'].forEach((k) => { if (map[k] == null || map[k] === true || (k === 'value' && map.of != null && map.total != null)) return; const v = pick(x, map[k]); if (v !== undefined) { o[k === 'rate' ? 'value' : k] = v; hit = true; } });
      return hit ? o : x; }
    let base = x, hit = picked;
    if (sh === 'graph') { const o = Object.assign({}, (x && typeof x === 'object' && !Array.isArray(x)) ? x : {}); ['nodes', 'links'].forEach((k) => { if (map[k] == null) return; const v = pick(x, map[k]); if (v !== undefined) { o[k === 'links' ? 'links' : 'nodes'] = v; hit = true; } }); if (!hit) return x; base = o; }
    else if (cont && map[cont] != null) { let v = pick(x, map[cont]); if (v !== undefined) {
      // a container that is a dict of things (workers keyed by id, nodes keyed by host) lists its entries as rows, the key as the name
      if (v && typeof v === 'object' && !Array.isArray(v) && cont !== 'text' && Object.keys(v).length && Object.keys(v).every((k) => v[k] && typeof v[k] === 'object')) v = Object.keys(v).map((k) => Object.assign({ name: k }, Array.isArray(v[k]) ? { values: v[k] } : v[k]));
      else if (v && typeof v === 'object' && !Array.isArray(v) && (cont === 'rows' || cont === 'events' || cont === 'points') && Object.keys(v).length) v = Object.keys(v).map((k) => ({ name: k, value: v[k] }));
      base = v; hit = true; } }
    /* the map's own words, beside the renames (the widget review, 2026-09-27: a list the source keeps is often the thing
       to COUNT, not to list - pipelines by decision, calls by capability, warnings by group):
         reverse: true            the list in the other order (a source that keeps its newest first, drawn as a trace)
         entries: '<field>'       a dict of plain values as rows {name, <field>} (a health summary {proxmox:'ok', ...} as pills)
         count: '<path>'          the rows grouped by that field and counted, largest first; sum: '<path>' adds that
                                  field instead of one per row (calls per caller)
         split: ['<path>', ...]   the rows as one series per named field (pass and fail per hour, stacked) - t: '<path>'
                                  names the time field */
    if (map.reverse && Array.isArray(base)) { base = base.slice().reverse(); hit = true; }
    if (map.entries && base && typeof base === 'object' && !Array.isArray(base) && Object.keys(base).length) {
      const ef = typeof map.entries === 'string' ? map.entries : 'value';
      base = Object.keys(base).filter((k) => base[k] == null || typeof base[k] !== 'object').map((k) => ({ name: k, [ef]: base[k] })); hit = true; }
    // fields: ['<path>', ...] - each row as its name and only those fields (numbers written as text read as numbers): the
    // columns a heat map or a table of a wide row should show
    if (Array.isArray(map.fields) && map.fields.length && Array.isArray(base)) { base = base.map((r) => { if (!r || typeof r !== 'object') return r; const o = { name: r.name ?? r.id ?? r.label ?? r.key };
      map.fields.forEach((f) => { const v = pick(r, f); o[String(f).split('.').pop()] = (typeof v === 'string' && v.trim() !== '' && isFinite(+v)) ? +v : v; }); return o; }); hit = true; }
    // where: {field: value | [values]} keeps only the rows that match - for any list, not only a count (the estate's nodes
    // on one plane: the work in flight, the runtimes)
    if (map.where && typeof map.where === 'object' && !Array.isArray(map.where) && Array.isArray(base) && sh !== 'level' && sh !== 'rate') { const wh = map.where;
      base = base.filter((r) => r && typeof r === 'object' && Object.keys(wh).every((k) => { const want = wh[k], v = pick(r, k); return Array.isArray(want) ? want.map(String).includes(String(v)) : String(v) === String(want); })); hit = true; }
    // span: 'day' | 'hour' | 'minute' - count by the time a row carries, cut to that span, oldest first (dreams per day)
    if (map.count && Array.isArray(base)) { const cutAt = { day: 10, hour: 13, minute: 16 }[String(map.span || '')] || 0;
      const agg = {}; base.forEach((r) => { if (!r || typeof r !== 'object') return; const k0 = pick(r, map.count); let k = (k0 == null || k0 === '') ? '(none)' : String(k0); if (cutAt) k = k.slice(0, cutAt); agg[k] = (agg[k] || 0) + (map.sum ? num(pick(r, map.sum)) : 1); });
      const ks = Object.keys(agg).sort(cutAt ? ((a, b) => (a < b ? -1 : a > b ? 1 : 0)) : ((a, b) => agg[b] - agg[a]));
      return keep((cont === 'rows' || cont === 'events' || cont === 'points') ? ks.map((k) => ({ name: k, value: agg[k] })) : ks.reduce((o, k) => { o[k] = agg[k]; return o; }, {})); }
    if (Array.isArray(map.split) && map.split.length && Array.isArray(base)) {
      const rw = base.filter((r) => r && typeof r === 'object'), o = {};
      map.split.forEach((f) => { o[String(f)] = rw.map((r, i) => ({ t: map.t ? pick(r, map.t) : (r.t ?? r.ts ?? r.time ?? r.hour ?? i), v: num(pick(r, f)) })); });
      return o; }
    const renames = keys.filter((k) => !fields.includes(k) && k !== cont && !MAP_WORDS.has(k) && !(sh === 'graph' && (k === 'nodes' || k === 'links')));
    // a fleet of series ({name: [{t, ping_ms, …}, …]}) takes the same renames inside each series: v <- the field that is the value
    if (renames.length && base && typeof base === 'object' && !Array.isArray(base) && Object.keys(base).length && Object.values(base).every((a) => Array.isArray(a) && a.length && a[0] && typeof a[0] === 'object')) {
      const o = {}; Object.keys(base).forEach((k) => { o[k] = base[k].map((r) => { if (!r || typeof r !== 'object') return r; const q = Object.assign({}, r); renames.forEach((rk) => { const v = pick(r, map[rk]); if (v !== undefined) { q[rk] = v; hit = true; } }); return q; }); }); base = o; }
    if (renames.length && Array.isArray(base) && base.some((r) => r && typeof r === 'object')) { base = base.map((r) => { if (!r || typeof r !== 'object') return r; const o = Object.assign({}, r); renames.forEach((k) => { const v = pick(r, map[k]); if (v !== undefined) { o[k] = v; hit = true; } });
      // a fleet of series ({name: [{t, ping_ms, …}]} listed as rows of values): the renames reach inside each row's points - v <- the field that is the value
      ['values', 'series', 'points', 'history'].forEach((f) => { if (Array.isArray(o[f]) && o[f].length && o[f][0] && typeof o[f][0] === 'object') o[f] = o[f].map((pt) => { const q = Object.assign({}, pt); renames.forEach((k) => { const v = pick(pt, map[k]); if (v !== undefined) { q[k] = v; hit = true; } }); return q; }); });
      return o; }); }
    else if (renames.length && sh === 'graph' && base && Array.isArray(base.nodes)) { base = Object.assign({}, base, { nodes: base.nodes.map((r) => { if (!r || typeof r !== 'object') return r; const o = Object.assign({}, r); renames.forEach((k) => { const v = pick(r, map[k]); if (v !== undefined) { o[k] = v; hit = true; } }); return o; }) }); }
    return keep(hit ? base : x);
  }
  // the record's read.map + read.range on the data a form is handed (the element and the sheet both go through here)
  function mapped(rec, form, data) {
    if (!rec || data === undefined) return data; const m = rec.read && rec.read.map, sh = DRAWN[canon(form)];
    let d = (m && typeof m === 'object' && Object.keys(m).length) ? applyMap(data, m, sh) : data;
    const rg = rec.read && rec.read.range;
    if (Array.isArray(rg) && rg.length === 2 && (sh === 'level' || sh === 'rate') && d && typeof d === 'object' && !Array.isArray(d)) d = Object.assign({}, d, { min: num(rg[0]), max: num(rg[1]) });
    const du = rec.draw && rec.draw.unit;   // the record's unit dresses a level the source gave bare
    if (du && (sh === 'level' || sh === 'rate')) { if (typeof d === 'number') d = { value: d, unit: String(du) }; else if (d && typeof d === 'object' && !Array.isArray(d) && !d.unit) d = Object.assign({}, d, { unit: String(du) }); }
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
    globe: () => ({ title: 'Events · last hour', pins: 4, night: true, sweep: true,
      points: [['Suez · shipping halt', 32.5, 30.5, 'var(--b-ac4)', 'godseye.shipping', '14:12 · 3 vessels rerouted'], ['Taiwan Strait · exercise', 120.5, 24.2, 'var(--b-ac3)', 'godseye.osint', '13:58 · 2 sources'], ['Nairobi · grid outage', 36.8, -1.3, 'var(--b-ac3)', 'godseye.infra', '13:41 · 40% load shed'], ['Reykjanes · quake 4.1', -22.4, 63.9, 'var(--b-ac5)', 'godseye.usgs', '13:30 · depth 6 km'],
        ['Houston · refinery fire', -95.4, 29.8, 'var(--b-ac4)', 'godseye.news', '13:02 · 2 units offline'], ['São Paulo · protest', -46.6, -23.5, 'var(--b-ac3)', 'godseye.osint', '12:44 · downtown'], ['Mumbai · monsoon warning', 72.9, 19.1, 'var(--b-ac)', 'godseye.weather', '12:20 · 48h'], ['Sydney · port strike', 151.2, -33.9, 'var(--b-ac3)', 'godseye.shipping', '11:50 · day 2']].map((p) => ({ name: p[0], lon: p[1], lat: p[2], col: p[3], meta: p[4], detail: p[5] })) }),
    files: () => [['/srv/vera/fabric.py', '12 KB', '14:41'], ['/srv/vera/gate.py', '4 KB', '14:38'], ['/notes/42-plan.md', '9 KB', '13:02'], ['/out/report.html', '31 KB', '12:48']].map((r) => ({ path: r[0], size: r[1], changed: r[2] })),
    checklist: () => [{ text: 'gate passed', done: true }, { text: 'sweep the estate', done: true }, { text: 'verify on the mirror' }, { text: 'land', due: 'today' }],
    kv: () => ({ status: 'serving', node: 'ct126', model: 'qwen3:30b', in_flight: 4, waiting: 'step 5' }),
    json: () => ({ ok: true, node: 'ct126', models: ['qwen3:30b', 'nomic-embed-text'], gate: { held: 1, capacity: 1 } }),
    diff: () => ({ diff: 'diff --git a/vera/gate.py b/vera/gate.py\n--- a/vera/gate.py\n+++ b/vera/gate.py\n@@ -12,3 +12,4 @@ def acquire():\n-    wait = 5\n+    wait = 2\n+    log.info("lease")\n     return lease' }),
    code: () => ({ path: 'vera/gate.py', code: 'def acquire(node):\n    lease = gate.take(node)\n    return lease\n' }),
    progress: () => ({ steps: [{ name: 'recall', status: 'done', elapsed_s: 1.2 }, { name: 'read', status: 'done', elapsed_s: 3.4 }, { name: 'author', status: 'running' }, { name: 'verify', status: 'waiting' }] }),
    status: () => ({ status: 'degraded', message: '1 of 4 checks needs a look', checks: [{ name: 'redis', status: 'ok' }, { name: 'neo4j', status: 'ok' }, { name: 'gate', status: 'warn' }, { name: 'ct126', status: 'ok' }] }),
    media: () => ({ url: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNjAiIGhlaWdodD0iOTAiPjxyZWN0IHdpZHRoPSIxNjAiIGhlaWdodD0iOTAiIGZpbGw9IiMyMjI2MzAiLz48Y2lyY2xlIGN4PSI4MCIgY3k9IjQ1IiByPSIyNCIgZmlsbD0iIzZlYThkOCIvPjwvc3ZnPg==', title: 'a drawing' }),
    error: () => ({ ok: false, error: 'ct130 did not answer in 25 s', detail: 'ConnectTimeout: 192.168.0.130:11435' }),
    month: () => { const t = new Date(), k = (n, h) => ymdOf(addDays(t, n)) + (h ? 'T' + h + ':00' : ''); return { events: [[0, '09:00', '10:00', 'digest', '#6ea8d8'], [0, '14:00', '15:30', 'loop v7 · step 5', '#a78bfa'], [1, '11:30', '12:00', 'sweep · fabric', '#5ec9a0'], [3, '', '', 'benchmark', '#e09a55'], [-2, '16:00', '17:00', 'review', '#e07a9a'], [6, '09:00', '09:30', 'digest', '#6ea8d8']].map((r, i) => ({ id: 's' + i, title: r[3], start: r[1] ? k(r[0], r[1]) : k(r[0]), end: r[2] ? k(r[0], r[2]) : k(r[0] + 1), all_day: !r[1], color: r[4], location: i === 1 ? 'the lab' : '' })) }; },
    schedule: () => FORM_SAMPLE.month(),
    calnav: () => ({ calendar: true }),
    vgraph: () => SAMPLE.graph(),
    markdown: () => '## Digest\nFour of four boots **clean**. The gate held `ct126` twice.\n- sweep the estate\n- verify on the mirror',
    pills: () => [['redis', 'ok'], ['neo4j', 'ok'], ['ollama', 'running'], ['ct130', 'down'], ['gate', 'ok']].map((r) => ({ name: r[0], status: r[1] })),
    context_graph: () => { const g = SAMPLE.graph(); return { nodes: g.nodes, rels: g.links.map((l) => ({ from: l.from, to: l.to, kind: l.kind })) }; },
    /* a structured graph's sample is a small, HONEST Explode contract — every card carries its span and the layer
       and engine that found it, every edge its resolution — because the gallery draws a form from its sample, and a
       sample that breaks the contract's own rules would teach the wrong shape to whoever copies it */
    structgraph: () => ({ kind: 'code', source: { path: 'vera/evolve/ollama_gate.py', label: 'ollama_gate.py' },
      layout: { direction: 'LR', mode: 'dependency' },
      layers: [{ id: 'symbols', label: 'Symbols', kind: 'entity', by: 'code_explode_core', on: true }],
      groups: [{ id: 'g1', label: 'ollama_gate.py' }],
      cards: [{ id: 'c1', kind: 'function', label: 'acquire', span: [40, 320], line: 3, line_end: 14, group: 'g1', layer: 'symbols', by: 'code_explode_core' },
              { id: 'c2', kind: 'function', label: 'release', span: [330, 520], line: 16, line_end: 24, group: 'g1', layer: 'symbols', by: 'code_explode_core' },
              { id: 'c3', kind: 'class', label: 'Lease', span: [540, 980], line: 26, line_end: 58, group: 'g1', layer: 'symbols', by: 'code_explode_core' }],
      edges: [{ from: 'c1', to: 'c3', label: 'RETURNS', resolution: 'exact', layer: 'symbols' },
              { from: 'c2', to: 'c3', label: 'TAKES', resolution: 'exact', layer: 'symbols' }],
      assessments: [] }),
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
    // ── the motion and iso boards' forms ──
    dial: () => ({ value: 62, min: 0, max: 100, unit: '%', note: 'requests in flight · gate' }),
    tank: () => ({ value: 742, min: 0, max: 1200, unit: 'GB', note: '742 GB of 1.2 TB', writing: true, rate: '4.1 MB/s' }),
    turbine: () => ({ value: 38.4, unit: 'tok/s', model: 'qwen3:30b', node: 'ct126 · V100', ctx: '16 384', queue: '2 waiting' }),
    scope: () => wave(60, 48, 34).map((p) => Math.round(p.v)),
    pulse: () => [['loop step', 9], ['cap call', 14], ['error', 2], ['git', 4]].flatMap((k) => Array.from({ length: k[1] }, (_, i) => ({ t: '2026-09-13T14:' + String(10 + i).padStart(2, '0'), kind: k[0], text: k[0] + ' ' + (i + 1) }))).concat([{ rate: 19.4 }]).slice(0, 29),
    pipes: () => ({ nodes: [['edge', .4, .4, '1.1 MB/s'], ['ct126', 3.4, .6, '18 MB/s'], ['pxstore', 4.4, 3.4, '31 MB/s'], ['ct121', 1.0, 3.2, '—']].map((n, i) => ({ id: n[0], u: n[1], v: n[2], rate: n[3] })), links: [['edge', 'ct126', 1], ['ct126', 'pxstore', 1], ['ct121', 'pxstore', 1], ['edge', 'ct121', 0]].map((e) => ({ from: e[0], to: e[1], value: e[2] })) }),
    comet: () => [[1.2, 'cap'], [3.4, 'loop'], [5.0, 'error'], [6.1, 'cap'], [8.8, 'loop'], [10.2, 'git'], [11.9, 'cap'], [13.0, 'loop'], [14.4, 'error'], [16.2, 'cap'], [17.5, 'loop'], [19.1, 'loop'], [20.8, 'git'], [22.4, 'loop']].map((e) => ({ t: '2026-09-13T' + String(Math.floor(e[0])).padStart(2, '0') + ':' + String(Math.round((e[0] % 1) * 60)).padStart(2, '0'), kind: e[1], text: e[1] + ' at ' + e[0] })),
    'split-flap': () => ({ lines: ['LOOP V7 · STEP 5 OF 9', 'WAITING ON YOU · 2 FILES'], highlight: [8, 14], note: 'the board is the form · the string is the config' }),
    orbit: () => [['aide', 0, 20, 9], ['coder', 0, 155, 7], ['prober', 0, 290, 6], ['dream', 1, 65, 8], ['narrator', 1, 200, 6], ['ingest', 1, 320, 5], ['sweeper', 2, 110, 6]].map((o) => ({ name: o[0], ring: o[1], angle: o[2], size: o[3] })),
    city: () => ({ nodes: [['ct126', 8, 62, 71, 0, 3, 1.5], ['ct121', 4, 41, 54, 0, 1, .4], ['ct118', 4, 18, 48, 0, 5.2, .3], ['pxstore', 16, 12, 42, 1, 5.6, 2.8], ['ct130', 4, 0, 0, 1, .6, 2.8], ['workstation', 12, 33, 51, 0, 3.2, 3.6]].map((n) => ({ id: n[0], cores: n[1], load: n[2], temp: n[3], alert: !!n[4], u: n[5], v: n[6], status: n[2] ? 'up' : 'down' })), links: [['ct126', 'pxstore'], ['ct121', 'pxstore'], ['ct118', 'pxstore'], ['ct130', 'pxstore'], ['workstation', 'pxstore']].map((e) => ({ from: e[0], to: e[1] })) }),
    shelf: () => [['qwen3:30b', 5.9], ['nomic-embed', 1.1], ['qwen3:8b', .4], ['whisper-sm', .2], ['kokoro-tts', .1]].map((m) => ({ name: m[0], size: m[1] })),
    stack: () => [['fabric re-embed', 'running', '9 steps · 2 asks · 14:38'], ['node prober backoff', 'landed', '6 steps · b787ec2'], ['watch-search rollout', 'landed', '11 steps · 7499c38'], ['ollama gate leases', 'landed', '4 steps · a9b7c84'], ['markets backtest', 'no change', '7 steps']].map((s) => ({ name: s[0], status: s[1], detail: s[2] })),
    stacks: () => ({ llm: 34, memory: 20, code: 14, fabric: 10, git: 8, web: 6, nlp: 5, other: 3 }),
    'meter-panel': () => ({ llm: 8, embed: 6, fabric: 5, git: 3, web: 4, nlp: 2, cap: 7, dream: 1 }),
    sweep: () => [['loop v7 step 4', 'loop'], ['fabric.status', 'cap'], ['ERR ct130', 'error'], ['code.read', 'cap'], ['gate lease', 'gate'], ['dream cycle', 'dream'], ['memory.select', 'cap'], ['pipeline review', 'run']].map((e, i) => ({ t: '2026-09-13T14:' + String(41 - i * 3).padStart(2, '0'), kind: e[1], text: e[0] })),
    conveyor: () => ({ stages: [{ name: 'adopt', done: true }, { name: 'branch', done: true }, { name: 'test', done: true }, { name: 'review', current: true }, { name: 'promote' }] }),
    activity: () => Array.from({ length: 18 }, (_, i) => ({ t: '2026-09-13T' + String(8 + Math.floor(i * .6)).padStart(2, '0') + ':00', kind: [5, 13].includes(i) ? 'error' : ['cap', 'loop', 'git', 'dream'][i % 4], text: 'event ' + (i + 1), duration: 4 + Math.round(Math.abs(Math.sin(i * 1.3)) * 30) })),
    notices: () => [['pxstore moves tonight', true], ['NIC swap on ct130', false], ['nightly review posted', false], ['new model resident', false], ['design review at 16:00', false]].map((n) => ({ t: '2026-09-13T09:00', text: n[0], new: n[1] })),
    library: () => [['Boot analysis.docx', 12], ['fabric_capabilities.py', 48], ['rollout-plan.md', 61], ['nightly-review.pdf', 34], ['sprite-sheet.png', 2], ['timings.csv', 3], ['handbook.docx', 90], ['notes.md', 8], ['budget.xlsx', 22], ['reports', 0, true]].map((r) => ({ name: r[0], pages: r[1], folder: !!r[2] })),
    pages: () => [['Home', true], ['Runbooks'], ['Fabric'], ['Loop Lab'], ['Markets'], ['Mesh']].map((p) => ({ name: p[0], home: !!p[1] })),
    approvals: () => ({ stages: [{ name: 'submitted', done: true }, { name: 'reviewed', done: true }, { name: 'approved', current: true }, { name: 'published' }] }),
    wiki: () => ({ open: 'runbooks', pages: 14 }),
    devices: () => [['node-a', 'online'], ['node-b', 'online'], ['node-c', 'online'], ['node-d', 'online'], ['node-e', 'stale'], ['node-f', 'online'], ['gateway', 'online', true]].map((d) => ({ name: d[0], status: d[1], gateway: !!d[2] })),
    notebook: () => [['# gate analysis', 'md'], ['digest = fabric.digest()', 'code'], ['four boots, four pulls', 'md'], ['if digest != stored: re_embed()', 'code'], ['→ running', 'code', true], ['the fix', 'md'], ['obs.provenance.last(4)', 'code'], ['results', 'md'], ['4 boots · 1 re-embed each', 'md']].map((c) => ({ title: c[0], kind: c[1], running: !!c[2] })),
    hosts: () => [['pve-01', ['vera', 'redis', 'neo4j', 'ct118']], ['pve-02', ['ct121', 'ct126', 'ollama', 'pxstore']], ['pve-03', ['ct130', 'mirror', 'bench', { name: 'sandbox', status: 'down' }]]].map((h) => ({ name: h[0], guests: h[1].map((g) => typeof g === 'string' ? { name: g, status: 'up' } : g) })),
    containers: () => [['vera-app', 'ct126', 'up'], ['ollama-gpu', 'ct126', 'up'], ['fabric-worker', 'ct126', 'up'], ['garage', 'ct126', 'stopped'], ['redis', 'ct126', 'up'], ['neo4j', 'ct126', 'up'], ['mirror', 'ct121', 'up'], ['bench', 'ct121', 'up'], ['loop-lab-dev', 'ct121', 'up'], ['session-a', 'ct121', 'up'], ['session-b', 'ct121', 'stopped'], ['operator', 'ct121', 'up']].map((c) => ({ name: c[0], host: c[1], status: c[2] })),
    models: () => [['qwen3:30b', 30, true], ['qwen3:8b', 8, true], ['llama3:4b', 4, false], ['nomic', 1.5, true], ['gemma:14b', 14, false], ['phi:3b', 3, false], ['deepseek:70b', 70, true]].map((m) => ({ name: m[0], params: m[1], resident: m[2] })),
    datasets: () => [['sessions', 0, 812], ['messages', 0, 640], ['entities', 0, 310], ['ontology', 0, 120], ['runs', 0, 90], ['pipelines', 1, 60], ['jobs', 1, 40], ['nodes', 1, 12], ['hosts', 1, 8], ['markets', 2, 400], ['bars', 2, 900], ['news', 2, 300]].map((d) => ({ name: d[0], tier: d[1], records: d[2] })),
    sandboxes: () => [['loop-lab-dev', 'running', true], ['session-a41c', 'running', false], ['session-7f0e', 'idle', false]].map((s) => ({ name: s[0], status: s[1], pinned: s[2] })),
    frame: () => ({ kind: 'terminal', title: 'vera@ct126 — tail -f vera_start.log', lines: [['vera@ct126:~$ tail -f vera_start.log', 'p'], ['14:38:02  fabric: corpus 4 412 docs', ''], ['14:38:05  fabric: digest unchanged', ''], ['14:38:05  fabric: 0 re-embeds', 'ac'], ['14:38:09  loop v7 · step 4 · code.author', 'dim'], ['▌', 'cur']] }),
    topology: () => ({ nodes: [['fabric', 0, 1, 1.5], ['neo4j', 0, 3, .8], ['redis', 0, 5, 2.2], ['router', 1, 2, 1.5], ['gate', 1, 4, 1.5], ['chat', 2, 3, 1.5], ['canvas', 2, 1, 2.4]].map((n) => ({ id: n[0], floor: n[1], u: n[2], v: n[3] })), links: [['fabric', 'router'], ['neo4j', 'router'], ['redis', 'gate'], ['router', 'chat'], ['gate', 'chat'], ['chat', 'canvas']].map((e) => ({ from: e[0], to: e[1] })), floors: ['fabric', 'routing', 'chat'] }),
    /* the multi-line sample: three nodes' latency over two hours, sampled every five minutes */
    lines: () => Object.fromEntries([['gpu-250', 24, 9], ['cpu-247', 40, 12], ['cpu-246', 58, 10]].map((s, k) => [s[0], wave(24 + k * 4, s[1], s[2]).slice(k * 4).map((p, i) => ({ t: 1790578800 + i * 300, v: Math.max(0, p.v) }))])),
    racks: () => SAMPLE.items(),
    rows: () => SAMPLE.items(), cards: () => SAMPLE.items(),
    temps: () => ({ 'gpu V100': 71, 'cpu pkg': 54, nvme0: 42, nvme1: 39, chipset: 48, ambient: 33 }),
  };
  // sample(formOrShape): the sample a form draws — a shape name gives the shape's, a form id its own (or its shape's)
  function sample(x) {
    const k = String(x || '').toLowerCase();
    if (SAMPLE[k] && !DRAWN[k]) return SAMPLE[k]();
    const f = canon(k); if (FORM_SAMPLE[f]) return FORM_SAMPLE[f]();
    const sh = DRAWN[f] || k; return (SAMPLE[sh] || SAMPLE.string)();
  }
  // the sample face, marked — a class on the inline sizes, a tag on the cell sizes
  // the tag sits in its own line above the face (it covered the first value - a temperature, a latency, a table's header);
  // a host that says "sample" in its own head (a dashboard tile's record chip) asks for no tag at all (tag === false)
  const sampleFace = (html, size, tag) => (size === 'xs' || size === 's') ? html.replace(/^<span class="(vw-xs|vw-chip)"/, '<span class="$1 vw-sampled" data-sample="1"') : '<div class="vw-sampled" data-sample="1">' + (tag === false ? '' : '<i class="vw-sampletag" title="sample data — nothing read yet">' + esc(typeof tag === 'string' && tag ? tag : 'sample') + '</i>') + html + '</div>';
  // the one figure a size below M shows
  // the chips a row-shaped answer makes at S: up to draw.limit (four) entries, each name · value (a number, a unit) or
  // name · status (a dot), then '+ n' - the Sizes board's node box, where a list is a line of chips
  function chipRow(form, d, opts) {
    const lim = Math.max(1, Math.min(8, (opts && opts.draw && +opts.draw.limit) || 4)); const unit = (opts && opts.draw && opts.draw.unit) || '';
    const inBytes = !!(opts && opts.draw && opts.draw.bytes === true);   // the values are byte counts: 98,198,093,824 reads 91.5 GB
    let ents = [];
    if (form === 'kv' && d && typeof d === 'object' && !Array.isArray(d)) ents = Object.keys(d).filter((k) => d[k] === null || typeof d[k] !== 'object').map((k) => ({ n: k, v: d[k] === null ? '—' : (typeof d[k] === 'boolean' ? (d[k] ? 'yes' : 'no') : d[k]) }));
    else if (form === 'numbers' || form === 'temps' || (form === 'pills' && keyed(d).length)) ents = keyed(d).map((x) => ({ n: x[0], v: x[1] }));
    else { const rw = rows(d); const firstNum = (r) => { const k = Object.keys(r).find((q) => typeof r[q] === 'number' && !/^(id|rssi_raw)$/.test(q)); return k ? r[k] : undefined; };
      ents = rw.map((r) => ({ n: nameOf(r), v: r.value ?? r.v ?? r.count ?? r.n ?? r.latency_ms ?? r.port ?? r.age_h ?? r.runs ?? r.size_gb ?? firstNum(r), st: r.status ?? r.state ?? r.health ?? r.decision ?? r.severity ?? '' })); }   // a row with no named value shows its first number
    ents = ents.filter((e) => e.n !== '' && e.n != null); if (!ents.length) return '';
    // a yes/no status (a backend up, a lane green) is the dot's colour and the name, in full - "r true · p… true" said nothing
    const yesNo = (s) => s === true || s === false || /^(true|false)$/i.test(String(s));
    const chip = (e) => (e.st !== '' && e.st != null && yesNo(e.st) && (e.v == null || e.v === '')) ? '<span class="vw-chip" title="' + esc(e.n + ' · ' + e.st) + '"><i class="st" style="background:' + stCol(e.st) + '"></i><b>' + esc(String(e.n).slice(0, 22)) + '</b></span>' : '<span class="vw-chip" title="' + esc(e.n + (e.v != null ? ' ' + fmt(e.v) : '') + (e.st ? ' ' + e.st : '')) + '">' + (e.st ? '<i class="st" style="background:' + stCol(e.st) + '"></i>' : '') + '<small>' + esc(String(e.n).slice(0, 22)) + '</small>' + (e.v != null && e.v !== '' ? '<b>' + esc(typeof e.v === 'number' ? (inBytes ? fmtBytes(e.v) : fmt(e.v) + (unit ? ' ' + unit : '')) : String(e.v).slice(0, 14)) + '</b>' : (e.st ? '<b>' + esc(String(e.st).slice(0, 12)) + '</b>' : '')) + '</span>';
    return '<span class="vw-chips">' + ents.slice(0, lim).map(chip).join('') + (ents.length > lim ? '<span class="vw-chip more">+ ' + (ents.length - lim) + '</span>' : '') + '</span>';
  }
  function figure(form, data) {
    form = canon(form); const d = dataFor(data, form);
    if (d == null) return '';
    if (CI_FORMS.test(form)) return ciFigure(form, d);
    if (form === 'lines') { const ls = linesOf(d); return ls.length ? ls.slice(0, 2).map((s) => (s.n ? esc(s.n) + ' ' : '') + fmt(s.pts[s.pts.length - 1].v)).join(' · ') : ''; }   /* the first two series' latest */
    if (DRAWN[form] === 'level') { const l = level(d); if (!l) return ''; const u = l.unit || ((l.bounded && form !== 'counter' && form !== 'hero' && l.lo === 0 && l.hi === 100) ? '%' : ''); return fmt(l.v) + (u && u !== '%' ? ' ' : '') + esc(u); }
    if (form === 'trace' || form === 'scatter') { const s = series(d); return s.length ? fmt(s[s.length - 1]) : ''; }
    if (form === 'thermo' || form === 'heat' || form === 'bars' || form === 'donut' || form === 'pills' || form === 'kv' || form === 'stack' || form === 'matrix') { const kv = keyed(d); return kv.length ? kv.length + ' · ' + esc(kv[0][0]) + ' ' + fmt(kv[0][1]) : ''; }
    if (form === 'stepper') { const st = (d.stages || d.steps || d); const arr = Array.isArray(st) ? st : []; const done = arr.filter((s) => s && (s.done || s.state === 'done' || s.status === 'done')).length; return arr.length ? done + ' / ' + arr.length : ''; }
    if (form === 'string') return esc(String(typeof d === 'string' ? d : (d.text ?? d.value ?? d.title ?? '')).slice(0, 24));
    // a structured graph at sticker size: what it found, which is the only thing that fits
    if (form === 'structgraph') { const c = (d && (d.contract || d.doc || d)) || {}; const n = (c.cards || []).length; return n ? n + (n === 1 ? ' card' : ' cards') : ''; }
    if (DRAWN[form] === 'ohlcv') { const r = rows(d); return r.length ? fmt(num(r[r.length - 1].close ?? r[r.length - 1].c)) : ''; }
    if (DRAWN[form] === 'matrix' || DRAWN[form] === 'calendar') { const r = rows(d); const n = r.length || ((d && typeof d === 'object') ? Object.keys(d).length : 0); return n ? n + ' rows' : ''; }
    const r = rows(d); return r.length ? r.length + (r.length === 1 ? ' row' : ' rows') : (typeof d === 'string' ? esc(d.slice(0, 24)) : '');
  }

  /* ── the renderers, at M (the gallery's unit); L and XL compose around them ─ */
  const R = {};
  R.trace = (d, H, o) => {
    if (multi(d).length > 1 && R.lines) return R.lines(d, H, o);   /* several series: one colour and a key each */
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
    const r = Math.max(16, (H - 8) / 2), c = 2 * Math.PI * r, s = H / 2 + 2, unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : '');
    return '<svg class="vw-svg" viewBox="0 0 ' + (s * 2) + ' ' + (s * 2) + '" style="height:' + H + 'px;width:auto"><circle cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="var(--bg2,#1a1c20)" stroke-width="6"/><circle cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="var(--acc,#5a9e8f)" stroke-width="6" stroke-dasharray="' + (f * c).toFixed(1) + ' ' + (c - f * c).toFixed(1) + '" transform="rotate(-90 ' + s + ' ' + s + ')" stroke-linecap="round"/><text x="' + s + '" y="' + s + '" text-anchor="middle" dominant-baseline="central" font-size="' + Math.max(10, r * 0.55) + '" fill="var(--text,#d8dce4)" font-family="var(--mono,ui-monospace,monospace)">' + esc(fmt(l.v)) + esc(unit) + '</text></svg>';
  };
  R.counter = (d, H) => {
    const l = level(d); if (!l) return EMPTY('a counter needs a value');
    const dl = l.delta != null ? '<span class="vw-delta ' + (num(l.delta) >= 0 ? 'up' : 'down') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + ' ' + esc(fmt(Math.abs(num(l.delta)))) + '</span>' : '';
    return '<div class="vw-hero"><b>' + esc(fmt(l.v)) + '</b><span class="vw-unit">' + esc(l.unit) + '</span>' + dl + '</div>';   // the hero fits the body it stands in (a 2×2 tile's is ~46 px) — no nominal floor from the size
  };
  R.bar = (d, H) => {
    const l = level(d); if (!l) return EMPTY('a meter needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1)));
    return '<div class="vw-meter"><span class="vw-track"><i style="width:' + (f * 100).toFixed(1) + '%"></i></span><b>' + esc(fmt(l.v)) + esc(l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : '')) + '</b>' + (l.delta != null ? '<small class="vw-delta ' + (num(l.delta) >= 0 ? 'up' : 'down') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + esc(fmt(Math.abs(num(l.delta)))) + '</small>' : '') + '</div>';
  };
  R.bars = (d, H, o) => {
    let kv = keyed(d); if (!kv.length) return EMPTY('no numbers to draw');
    if (o && o.draw && o.draw.sort !== false) kv = kv.slice().sort((a, b) => b[1] - a[1]);
    kv = kv.slice(0, (o && o.draw && o.draw.limit) || 12);
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1, W = 300, bw = W / kv.length;
    /* the colour says something only when it can: a palette the record asks for, else the status when every bar is named by one */
    const bFall = (c) => String(c).replace(/^var\(--b-(ac|ac2|ac3|ac4|t3|dv[1-7])\)$/, (m, n) => 'var(--b-' + n + ',' + ({ ac: '#6ea8d8', ac2: '#5ec9a0', ac3: '#e09a55', ac4: '#e06060', t3: '#6b7280', dv1: '#866ec5', dv2: '#54a863', dv3: '#3585c9', dv4: '#bb881a', dv5: '#b95c88', dv6: '#00aba4', dv7: '#bd6533' })[n] + ')');
    const bPal = (o && o.draw && o.draw.palette) ? palOf(o) : null, bSt = kv.every((x) => stCol(x[0]) !== B.t3);
    const bCol = (i, x) => bPal ? bFall(bPal(i, Math.abs(x[1]), hi, x[0])) : (bSt ? bFall(stCol(x[0])) : 'var(--acc,#5a9e8f)');
    return '<svg class="vw-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="height:' + H + 'px">' + kv.map((x, i) => { const h = Math.max(1, (Math.abs(x[1]) / hi) * (H - 14)); return '<rect' + itemAttr({ name: x[0], value: x[1] }, 'bar') + ' x="' + (i * bw + 2).toFixed(1) + '" y="' + (H - 12 - h).toFixed(1) + '" width="' + Math.max(1, bw - 4).toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2" fill="' + bCol(i, x) + '"><title>' + esc(x[0]) + ' · ' + esc(fmt(x[1])) + '</title></rect><text x="' + (i * bw + bw / 2).toFixed(1) + '" y="' + (H - 2) + '" text-anchor="middle" class="vw-svgt" fill="var(--dim2,#8a92a0)">' + esc(String(x[0]).slice(0, 6)) + '</text>'; }).join('') + '</svg>';
  };
  R.thermo = (d, H, o) => {
    const kv = keyed(d); if (!kv.length) return EMPTY('no numbers to draw');
    const hi = Math.max(...kv.map((x) => Math.abs(x[1]))) || 1;
    // as many rows as the body holds (~18 px a row); draw.limit still says fewer
    const lim = Math.min(8, (o && o.draw && +o.draw.limit) || 8, Math.max(2, Math.floor(((H || 150) + 4) / 18)));
    return '<div class="vw-therm">' + kv.slice(0, lim).map((x) => '<div><label title="' + esc(x[0]) + '">' + esc(x[0]) + '</label><span><i style="width:' + (100 * Math.abs(x[1]) / hi).toFixed(1) + '%"></i></span><b>' + esc(fmt(x[1])) + '</b></div>').join('') + '</div>';
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
  /* the donut is as tall as its body and no wider than it is tall: the ring sized to the body (never past 140 px), its
     total in the middle, the legend beside it as a short column of name + value + share - or under it when the tile
     is narrow - so a donut tile can be three columns wide instead of four (the owner: "donut/ring charts take up too
     much horizontal space"). Each part is a block: hovered it lights with its share, clicked it opens where it lives. */
  R.donut = (d, H, o) => {
    const kv = keyed(d).filter((x) => x[1] !== 0).slice(0, 8); if (!kv.length) return EMPTY('parts need { name: number }');
    const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1, D = Math.max(40, Math.min(140, (H || 96) - 6)), r = D / 2 - 6, c = 2 * Math.PI * r, s = D / 2; let acc = 0;
    // palette status colours a part by its name (running green, stopped red, pending amber) - a share of states reads at a glance
    const cols0 = [0, 1, 2, 3, 4, 5, 6].map((i) => DV(i)), dPal = o && o.draw && o.draw.palette;
    /* palette status - or no palette and every part named by a status word (running · stopped) - colours a part by its name */
    const cols = (dPal === 'status' || (!dPal && kv.every((x) => stCol(x[0]) !== B.t3))) ? kv.map((x, i) => { const sc = stCol(x[0]); return sc === B.t3 ? cols0[(i + 5) % cols0.length] : sc; }) : cols0;
    const sw = Math.max(6, Math.round(D / 11)), pc = (v) => Math.round(Math.abs(v) / tot * 100) + '%';
    const arcs = kv.map((x, i) => { const f = Math.abs(x[1]) / tot; const el = '<circle class="vw-arc" data-b="p' + i + '"' + itemAttr({ name: x[0], value: x[1], share: Math.round(f * 1000) / 10, of: tot }, 'slice') + ' data-tip="' + esc(x[0] + '\n' + fmt(x[1]) + ' · ' + pc(x[1]) + ' of ' + fmt(tot)) + '" cx="' + s + '" cy="' + s + '" r="' + r + '" fill="none" stroke="' + cols[i % cols.length] + '" stroke-width="' + sw + '" stroke-dasharray="' + Math.max(0, f * c - 2).toFixed(1) + ' ' + (c - f * c + 2).toFixed(1) + '" stroke-dashoffset="' + (-acc * c).toFixed(1) + '" transform="rotate(-90 ' + s + ' ' + s + ')"></circle>'; acc += f; return el; }).join('');
    const mid = '<text x="' + s + '" y="' + s + '" class="vw-dtot" text-anchor="middle" dominant-baseline="central">' + esc(fmt(tot)) + '</text>';
    return '<div class="vw-donut"><svg class="vw-svg" viewBox="0 0 ' + D + ' ' + D + '" style="height:' + D + 'px;width:' + D + 'px;flex:none">' + arcs + mid + '</svg><div class="vw-legend vw-legend-col">' + kv.map((x, i) => '<span data-b="p' + i + '"' + itemAttr({ name: x[0], value: x[1], share: Math.round(Math.abs(x[1]) / tot * 1000) / 10, of: tot }, 'slice') + '><i style="background:' + cols[i % cols.length] + '"></i><em>' + esc(x[0]) + '</em><b>' + esc(fmt(x[1])) + '</b><small>' + pc(x[1]) + '</small></span>').join('') + '</div></div>';
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
  R.log = (d, H) => {
    const rw = rows(d); if (!rw.length || !rw.some((r) => r.text != null || r.msg != null || r.message != null || r.line != null || r.title != null)) return EMPTY('a log needs rows with text');
    const n = Math.max(3, Math.min(40, Math.floor(((H || 70) - 2) / 14)));   // the lines that fit the body (14 px each)
    return '<div class="vw-log">' + rw.slice(-n).map(logLine).join('') + '</div>';
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
  R.list = (d, H) => {
    const rw = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const n = Math.max(2, Math.min(24, Math.floor(((H || 70) - 2) / 16)));
    if (str.length) return '<div class="vw-list">' + str.slice(0, n).map((s) => '<div><b>' + esc(s) + '</b></div>').join('') + '</div>';
    if (!rw.length) return EMPTY('no rows');
    return '<div class="vw-list">' + rw.slice(0, n).map((r) => '<div><b>' + esc(String(r.name ?? r.title ?? r.label ?? r.id ?? '')) + '</b><small>' + esc(String(r.meta ?? r.when ?? r.status ?? r.count ?? r.value ?? '')) + '</small></div>').join('') + '</div>';
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
  R.kv = (d, H, o2) => {
    const o = (d && typeof d === 'object' && !Array.isArray(d)) ? d : null; if (!o) return EMPTY('nothing to list');
    const all = Object.keys(o).filter((k) => typeof o[k] !== 'object' || o[k] === null); if (!all.length) return EMPTY('no plain values to list');
    const n = Math.max(2, Math.min((o2 && o2.draw && o2.draw.limit) || 12, Math.floor(((H || 70) - 2) / 14))); const ks = all.slice(0, n);   // the pairs that fit
    return '<div class="vw-log vw-kv">' + ks.map((k) => '<div><span class="k">' + esc(k) + '</span><span>' + esc(String(o[k])) + '</span></div>').join('') + (all.length > n ? '<div class="more">+ ' + (all.length - n) + ' more</div>' : '') + '</div>';
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
  R.diagram = (d) => {
    // {nodes, links} → the ONE diagram renderer (<vera-mermaid>) through a slot hydrate() fills when the element is defined
    // (the chat-era pipes form; the board's pipes are the iso graph below, which hands a draw.engine of mermaid back here)
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
  /* THE EXPLODE CONTRACT, AS A WIDGET (the canvas's final form §3.6a). The data IS the contract — {kind, source,
     layout, layers, groups, cards, edges, assessments} — and the face is the estate's own renderer, which the chat,
     the canvas and the fabric panel all already host: one contract, one drawing of it. A widget wrapper buys the
     contract the things a bespoke drawer never had: five sizes, a source it can read again (code.explode ·
     nlp.explode.prose), and a place in any board a widget can stand in.
     At the small sizes a diagram is not readable, so they answer what it FOUND; from m up the renderer is mounted
     into a slot (hydrate below), the way the context graph and mermaid already are. */
  const sgDoc = (d) => { const c = (d && typeof d === 'object') ? (d.contract || d.doc || d) : null;
    return (c && (Array.isArray(c.cards) || Array.isArray(c.edges))) ? c : null; };
  const sgCounts = (c) => ({ cards: (c.cards || []).length, edges: (c.edges || []).length,
    layers: (c.layers || []).filter((l) => l && l.on !== false).length, kind: String(c.kind || '') });
  R.structgraph = (d, H, o) => {
    o = o || {}; const c = sgDoc(d);
    if (!c) return EMPTY('a structured graph needs an Explode contract');
    const n = sgCounts(c);
    if (!n.cards && !n.edges) return EMPTY('nothing was found in this source');
    const words = n.cards + (n.cards === 1 ? ' card' : ' cards') + (n.edges ? ' · ' + n.edges + (n.edges === 1 ? ' relation' : ' relations') : '');
    /* xs and s never reach here: the element draws every form's sticker and chip itself, from glyphOf + figure()
       (below), and a form inventing its own small face would be the one that looked unlike all the others. */
    const attrs = ['renderer="struct"'];
    if (o.mode || (c.layout && c.layout.mode)) attrs.push('mode="' + esc(String(o.mode || c.layout.mode)) + '"');
    /* the counts stand in until the renderer is mounted — NOT the empty state: `wempty` is how the element and its
       tests say "this form has no face", and this one has a face, it is just a moment away from being drawn */
    return '<div class="vw-sg" data-sg="' + esc(JSON.stringify(c)) + '" data-sg-attrs="' + esc(attrs.join(' ')) +
      '" style="position:relative;height:' + Math.max(H, 180) + 'px"><span class="vw-sg-w">' + esc(words) + '</span></div>';
  };
  R.composite = (d, H, o) => {
    const rec = (o && o.record) || {}; const kids = Array.isArray(rec.children) ? rec.children : [];
    if (!kids.length) return EMPTY('a composite needs children');
    const layout = rec.layout || 'grid', kd = (o && o.kids) || {}, chip = layout === 'rail' || layout === 'report';
    // the frame's height (opts.height is the element's measured body) shared among the rows of slots: a 2 × 2 of four
    // children gets two rows, each slot a fixed height, its body scrolling — the composite fills its tile and never grows it
    const shown = kids.slice(0, 12), wide = !!(o && o.width && o.width >= 560);
    // a 'rows' composite is one column (its CSS stacks the slots): each slot takes its share of the height, not all of it
    const ncol = (chip || layout === 'rows') ? 1 : Math.max(1, Math.min(4, (rec.draw && +rec.draw.cols) || (wide && shown.length >= 3 ? 3 : 2))), nrows = Math.max(1, Math.ceil(shown.length / ncol));
    // a slot's share of the frame follows what its child needs: a figure (counter, ring, kv) takes less than a list or a
    // chart; each row of slots is as tall as its neediest child, the measured body split by those weights; the last row
    // fills its width (three children are two slots and a wide one, not a slot and a hole) - no half-empty row, no scrolling slot
    const need = (c) => { const r0 = (c && c.record && typeof c.record === 'object') ? c.record : {}, f = canon(r0.form || ''); if (f === 'kv' && r0.read && r0.read.map && Array.isArray(r0.read.map.keys) && r0.read.map.keys.length >= 4) return 1.15; /* four key/values read as a list: a list's share */ return /^(counter|string|hero|level|ring|dial|gauge|meter|pills|numbers|kv|dots)$/.test(f) ? 0.62 : /^(rows|list|table|log|feed|cards|files|checklist|temps|thermo|bullet|ranked|hosts)$/.test(f) ? 1.15 : 1; };
    // the least a slot can be and still show its child whole: a figure, three rows, a chart's floor - a row of slots
    // gets at least its tallest floor, the rest of the body is shared by weight (a list over a figure)
    const floorOf = (c) => { const f = canon((c && c.record && typeof c.record === 'object' && c.record.form) || ''); return /^(counter|hero|string|pills|numbers)$/.test(f) ? 62 : /^(kv)$/.test(f) ? 78 : /^(rows|list|table|log|feed|temps|thermo|files|checklist|ranked|bullet)$/.test(f) ? 88 : /^(ring|dial|gauge)$/.test(f) ? 96 : 72; };
    /* a list (rows, a table, a log, chips ...) takes a whole row of the composite - in a quarter of the card it showed two
       words of each line (owner, 2026-09-27); the figures and charts pack their rows above, in their order */
    const isList = (c) => { if (ncol < 2) return false; const r = (c && c.record && typeof c.record === 'object') ? c.record : {}, f = canon(r.form || ''); return /^(rows|list|table|log|feed|cards|files|checklist|timeline|lane|people|links|spark-table|temps|thermo|ranked|hosts|pills|stack)$/.test(f) || (f === 'kv' && !!(r.read && r.read.map && Array.isArray(r.read.map.keys) && r.read.map.keys.length >= 4)); };
    const packed = [], lists = []; shown.forEach((c, i) => (isList(c) ? lists : packed).push({ c, i }));
    const rowsP = []; for (let q = 0; q < packed.length; q += ncol) rowsP.push(packed.slice(q, q + ncol)); lists.forEach((x) => rowsP.push([x]));
    const plan = []; rowsP.forEach((rw, ri) => { const base = Math.floor(ncol / rw.length), extra = ncol - base * rw.length; rw.forEach((x, j) => plan.push({ c: x.c, i: x.i, ri, sp: base + (j < extra ? 1 : 0) })); });
    const wts = rowsP.map((rw) => Math.max(...rw.map((x) => need(x.c)))), fls = rowsP.map((rw) => Math.max(...rw.map((x) => floorOf(x.c)))); const wsum = wts.reduce((a, b) => a + b, 0) || 1;
    const bodyH = (o && o.height && !chip) ? o.height : 0, free = Math.max(0, bodyH - (rowsP.length - 1) * 8 - fls.reduce((a, b) => a + b, 0));
    const slotHOf = (ri) => bodyH ? Math.max(44, (fls[ri] || 44) + Math.floor(free * (wts[ri] || 1) / wsum)) : 0;
    return '<div class="vw-comp vw-comp-' + esc(layout) + '"' + (!chip && ncol !== 2 ? ' style="grid-template-columns:repeat(' + ncol + ',1fr)"' : (chip && o && o.height ? ' style="height:' + o.height + 'px"' : '')) + '>' + plan.map(({ c, i, ri, sp }) => {
      const slotH = slotHOf(ri), kidH = slotH ? Math.max(24, slotH - 36) : Math.max(44, Math.round(H * 0.8));
      const slotStyle = (slotH || sp > 1) ? ' style="' + (slotH ? 'height:' + slotH + 'px;' : '') + (sp > 1 ? 'grid-column:span ' + sp + ';' : '') + '"' : '';
      const r0 = c && typeof c.record === 'object' ? c.record : null; const slot = String((c && c.slot) || String.fromCharCode(97 + i));
      if (!r0) return '<div class="vw-slot" data-slot="' + esc(slot) + '"><small class="wempty">' + esc(String(c && c.record || '')) + '</small></div>';
      const n = normalise(r0); const k = kd[slot]; const wasRead = !!(k && typeof k === 'object' && k.__read); const data = r0.data !== undefined ? r0.data : (wasRead ? k.data : k); const kopts = { record: n, draw: n.draw, bare: true, sample: wasRead ? false : undefined, textK: o && o.textK, width: (o && o.width && !chip) ? Math.max(120, Math.floor((o.width - (ncol - 1) * 8) * sp / ncol) - 16) : undefined };
      // a child whose reading is still on its way says so - its slot never shows the form's sample values as though they
      // were the estate's (the review found "serving · ct126 · qwen3:30b" standing in for the narrator, the identity
      // server, OpenClaw and telegram while their reads were queued)
      if (k && k.__pending && !wasRead && r0.data === undefined) return chip
        ? '<div class="vw-slot vw-slot-row vw-pending" data-slot="' + esc(slot) + '"><span class="k" title="' + esc(n.title || n.form) + '">' + esc(n.title || n.form) + '</span><span class="vw-slot-c"><i class="vw-kread">reading ' + esc(n.source && !/^\$subject/.test(n.source) ? n.source : '') + '…</i></span></div>'
        : '<div class="vw-slot vw-pending" data-slot="' + esc(slot) + '"' + slotStyle + '><span class="vw-slot-h">' + esc(n.title || n.form) + '<i class="vw-kread">reading…</i></span><div class="vw-slot-b"></div></div>';
      const kHave = wasRead && k.data !== undefined && !isEmpty(dataFor(mapped(n, n.form, k.data), canon(n.form)));   // what the child's form would draw of the answer
      const kerr = wasRead && k.err ? '<i class="vw-kerr" title="' + esc(k.err) + '">' + (kHave ? 'last reading' : 'read failed') + '</i>' : (wasRead && !kHave ? '<i class="vw-kempty">read · empty</i>' : '');
      const stale = wasRead && k.err && kHave ? ' vw-stale' : '';
      /* a report's list or key/values is a BLOCK with its rows (owner, 2026-09-27: "most of the chips in the table cards like
         estate health, backups and node properties could be more informative" - a list drawn as a chip said "4 rows"); only
         a single figure stays a chip in its row */
      if (chip && /^(rows|list|table|log|feed|cards|files|checklist|timeline|lane|kv|pills|ranked|hosts|temps|thermo|people|links)$/.test(canon(n.form))) {
        const tk = (o && o.textK) || 1, lim = +(n.draw && n.draw.limit) || 4, keysN = (n.read && n.read.map && Array.isArray(n.read.map.keys)) ? n.read.map.keys.length : 4;
        const bh = Math.round((canon(n.form) === 'kv' ? keysN * 19 : canon(n.form) === 'pills' ? 52 : lim * 31 + 56) * tk);
        return '<div class="vw-slot vw-slot-block' + stale + '" data-slot="' + esc(slot) + '"><span class="vw-slot-h">' + esc(n.title || n.form) + kerr + '</span><div class="vw-slot-b">' + draw(n.form, data, 'm', Object.assign({ height: bh, title: n.title }, kopts, { width: (o && o.width) ? Math.max(160, o.width - 20) : undefined })) + '</div></div>'; }
      if (chip) return '<div class="vw-slot vw-slot-row' + stale + '" data-slot="' + esc(slot) + '"><span class="k" title="' + esc(n.title || n.form) + '">' + esc(n.title || n.form) + '</span><span class="vw-slot-c">' + draw(n.form, data, 's', Object.assign({}, kopts, { title: '' })) + kerr + '</span></div>';
      const fig = /^(counter|hero|string|level|ring|meter|gauge|dial|tank|numbers)$/.test(canon(n.form)) ? '' : figure(n.form, mapped(n, n.form, data === undefined && !wasRead ? sample(n.form) : data));
      return '<div class="vw-slot' + stale + '" data-slot="' + esc(slot) + '"' + slotStyle + '><span class="vw-slot-h">' + esc(n.title || n.form) + (fig ? '<b>' + fig + '</b>' : '') + kerr + '</span><div class="vw-slot-b">' + draw(n.form, data, 'm', Object.assign({ height: kidH, title: n.title }, kopts)) + '</div></div>'; }).join('') + '</div>';
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
  // a boolean status reads too (a test lane's ok, a workflow's active, a gate's enabled): true is good, false wants a look
  const stCol = (s) => { const k = String(s == null ? '' : s).toLowerCase(); return /^(ok|up|green|pass|passed|running|healthy|done|serving|online|live|merged|here|attached|good|true|yes|active|enabled|promoted|adopted|success)$/.test(k) ? B.ac2 : /^(warn|paused|degraded|amber|waiting|review|queued|stale|expiring|idle|away|busy|false|no|pending|planned)$/.test(k) ? B.ac3 : /^(down|fail|failed|red|error|stopped|dead|err|off|offline|timeout)$/.test(k) ? B.ac4 : B.t3; };
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
  /* ══ THE TABLE FAMILY (Notes/42 defect 50: "might also need to define some table widgets") ═══════════════════════════
     One grid under four forms. The columns come from the record's read.map (its renames, in the order written: name ←
     hostname, value ← load → columns name · value), else draw.columns, else the envelope's own keys. The header sorts
     (a click sets the column, a second flips the direction); the row count is the SIZE's: S is a count chip (the figure),
     M shows 4 rows, L 8 with the footer (the order, the page, ‹ ›), XL the whole table paged at 12 with a search box.
       table  the sortable grid (the Widgets board's Nodes)      rows   the same rows without the header — a composite's child
       cards  each row a card (name · two fields · a status dot)  temps  the WidgetConfig board's Temp list: named values as
                                                                        rows, a bar to the max, hottest first, coloured by band */
  const TABLE_FORMS = new Set(['table', 'rows', 'cards', 'temps', 'files', 'list']);
  const TABLE_ROWS = { xs: 1, s: 1, m: 4, l: 8, xl: 12 };
  // the rows a frame holds (opts.height is the element's measured body, or a composite slot's): the table shows that many
  // and pages the rest — the size's count when nothing measured it yet
  const fitRows = (o, size, rowH, chrome) => (o && o.height ? Math.max(2, Math.floor((o.height - chrome) / rowH)) : (TABLE_ROWS[size] || 4));
  // the columns the record asks for: read.map's renames (not the container, not the shape's own fields), then draw.columns,
  // then the row's plain keys — never more than `max`, never a key the row lacks
  const tableCols = (rw, o, max) => {
    const r0 = rw[0] || {}; const has = (c) => rw.some((r) => r && r[c] !== undefined);
    const m = (o && o.record && o.record.read && o.record.read.map) || (o && o.map) || null; const skip = new Set(['rows', 'items', 'series', 'values', 'events', 'nodes', 'links', 'stages', 'parts', 'bars', 'cells', 'days', 'text', 'points', 'children', 'panel', 'value', 'min', 'max', 'unit', 'rate']);
    let cols = (o && o.draw && Array.isArray(o.draw.columns)) ? o.draw.columns.filter(has) : [];   // the record's own columns first
    if (!cols.length && m) cols = Object.keys(m).filter((k) => !skip.has(k) && has(k));
    if (!cols.length) cols = Object.keys(r0).filter((k) => typeof r0[k] !== 'object' && !/^(_|__)/.test(k));
    return cols.slice(0, max || 6);
  };
  const tableSort = (rw, cols, o) => { const sortBy = String(ui(o, 'sort', (o && o.draw && o.draw.sort) || '')), dir = +ui(o, 'dir', -1) || -1; if (sortBy && cols.includes(sortBy)) rw = rw.slice().sort((a, b) => { const x = a[sortBy], y = b[sortBy]; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x ?? '').localeCompare(String(y ?? ''))) * dir; }); return { rw, sortBy, dir }; };
  const tableCell = (r, c, lit) => { const v = r[c]; const n = typeof v === 'number'; const hot = lit != null && n && v >= num(lit); const st = !n && /^(status|state|health)$/i.test(c), yn = typeof v === 'boolean'; return '<span class="c' + (n ? ' num' : '') + (hot ? ' dn' : '') + '" title="' + esc(c + ' \u00b7 ' + String(v ?? '')) + '">' + (st ? '<i class="st" style="background:' + stCol(v) + '"></i>' : '') + (yn ? (st ? '' : (v ? '\u2713' : '\u2013')) : esc(v == null ? '' : (n ? fmt(v) : (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v) ? ago(v) : String(v))))) + '</span>'; };   /* a yes/no is a mark (a dot for a status, else a tick or a dash) - it read 'true' / 'false' */
  const gridCols = (cols) => cols.map((c, i) => i ? 'minmax(40px,auto)' : '1fr').join(' ');
  R.table = (d, H, o) => {
    let all = rows(d); if (!all.length) return EMPTY('no rows');
    const size = (o && o.size) || 'm'; const cols = tableCols(all, o, size === 'xl' ? 8 : 6); const lit = o && o.draw && o.draw.lit != null ? o.draw.lit : null;
    const q = String(ui(o, 'q', '')).trim().toLowerCase(); if (q) all = all.filter((r) => cols.some((c) => String(r[c] ?? '').toLowerCase().includes(q)));
    const lim = (o && o.draw && o.draw.limit) || fitRows(o, size, 17, 40 + (size === 'xl' ? 24 : 0)); const { rw, sortBy, dir } = tableSort(all, cols, o);
    const page = Math.max(0, +ui(o, 'page', 0) || 0), pages = Math.max(1, Math.ceil(rw.length / lim)), pg = Math.min(page, pages - 1), shown = rw.slice(pg * lim, pg * lim + lim);
    const head = '<div class="vb-dgr h" style="grid-template-columns:' + gridCols(cols) + '">' + cols.map((c) => '<button class="' + (sortBy === c ? 'on' : '') + '"' + set('sort', c) + (sortBy === c ? ' data-vb-set2="dir:' + (-dir) + '"' : ' data-vb-set2="dir:-1"') + '>' + esc(c.replace(/_/g, ' ')) + '<span>' + (sortBy === c ? (dir < 0 ? '↓' : '↑') : '') + '</span></button>').join('') + '</div>';
    const body = shown.map((r) => '<div class="vb-dgr"' + itemAttr(r, 'row') + ' style="grid-template-columns:' + gridCols(cols) + '">' + cols.map((c) => tableCell(r, c, lit)).join('') + '</div>').join('');
    const search = size === 'xl' ? '<div class="vb-tsearch"><span>⌕</span><input type="search" value="' + esc(q) + '" placeholder="find in ' + all.length + ' rows" data-vb-input="q"></div>' : '';
    const foot = size === 'l' || size === 'xl' || pages > 1 ? '<div class="vb-dgf"><span>' + (sortBy ? 'sorted by ' + esc(sortBy) + (dir < 0 ? ' · high first' : ' · low first') : rw.length + ' rows') + '</span><span style="margin-left:auto">' + (rw.length ? (pg * lim + 1) + '–' + Math.min(rw.length, pg * lim + lim) + ' of ' + rw.length : '0 of 0') + '</span><button' + set('page', Math.max(0, pg - 1)) + '>‹</button><button' + set('page', Math.min(pages - 1, pg + 1)) + '>›</button></div>' : '';
    return wrap('table', search + head + (body || '<span class="vb-lbl">' + (q ? 'nothing matches "' + esc(q) + '"' : 'no rows') + '</span>') + foot, 'vb-tf');
  };
  /* THE LIST CARD (owner, 2026-09-28: "i still think the chip tables need to be vastly improved"). A row said a name, a word
     and a dot. Now each says what it is and how it stands at a glance: its status as a dot and - room allowing - a
     coloured word (a yes/no in the source's own words: running / stopped, enabled / disabled); its name, with the detail
     under it (the error, the branch, the subject, a time as "3 h ago"); its figure on the right with its unit, over a bar
     that is its share of the largest. Problems come first. Above the rows, when they carry a status, a bar of the statuses
     with their counts - a click shows that status alone. "+ n more" opens the rest in the drawer. */
  const ST_KEYS = /^(status|state|health|severity|level|result|decision|running|enabled|ok|active|online|last_ok)$/i;
  const DET_KEYS = ['error', 'last_error', 'last_defer', 'detail', 'details', 'subject', 'reason', 'note', 'message', 'msg', 'branch', 'kind', 'type', 'role', 'model', 'host', 'node', 'path', 'description', 'summary', 'title', 'label'];
  const isoTime = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v);
  const ago = (v) => { const t = Date.parse(v); if (!isFinite(t)) return String(v); const s = Math.round((Date.now() - t) / 1000), a = Math.abs(s); const f = a < 60 ? a + ' s' : a < 3600 ? Math.round(a / 60) + ' min' : a < 86400 ? Math.round(a / 3600) + ' h' : Math.round(a / 86400) + ' d'; return s < 0 ? 'in ' + f : f + ' ago'; };
  const YES_NO = { running: ['running', 'stopped'], enabled: ['enabled', 'disabled'], active: ['active', 'inactive'], online: ['online', 'offline'], ok: ['ok', 'failing'], last_ok: ['ok', 'failed'], pinned: ['pinned', 'not pinned'], paused: ['paused', 'running'], reachable: ['reachable', 'unreachable'] };
  const yesNoWord = (s, col) => { const k = String(s == null ? '' : s).toLowerCase(); if (k !== 'true' && k !== 'false') return String(s == null ? '' : s); const w = YES_NO[String(col || '').toLowerCase()]; return w ? w[k === 'true' ? 0 : 1] : (k === 'true' ? 'yes' : 'no'); };
  const stRank = (w) => { const c = stCol(w); return c === B.ac4 ? 0 : c === B.ac3 ? 1 : c === B.t3 ? 2 : 3; };
  const srcCol = (o, c) => { const m = o && o.record && o.record.read && o.record.read.map; return (m && typeof m[c] === 'string') ? m[c].split('.').pop() : c; };
  R.rows = (d, H, o) => {
    const all = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const rw0 = all.length ? all : str.map((s) => ({ name: s })); if (!rw0.length) return EMPTY('no rows');
    const size = (o && o.size) || 'm', tk = (o && o.textK) || 1, Wd = (o && o.width) || 0, dr = (o && o.draw) || {};
    const cols = tableCols(rw0, o, 6), r0 = rw0[0] || {};
    const nameC = cols.find((c) => /^(name|title|label|id|host|hostname|node|branch|subject|key|path)$/i.test(c)) || cols[0] || 'name';
    const stC = cols.find((c) => c !== nameC && ST_KEYS.test(c)) || Object.keys(r0).find((c) => c !== nameC && ST_KEYS.test(c) && r0[c] != null && typeof r0[c] !== 'object') || '';
    /* the figure: the row's 'value' when it has one (the shape's own field, which the column list leaves out), else a number or a time among its fields */
    const keys0 = Array.from(new Set(['value'].concat(cols, Object.keys(r0).filter((k) => r0[k] == null || typeof r0[k] !== 'object'))));
    const valC = keys0.find((c) => c !== nameC && c !== stC && rw0.some((r) => r[c] != null && r[c] !== '' && (typeof r[c] === 'number' || isoTime(r[c]) || c === 'value'))) || '';
    const detC = dr.detail || DET_KEYS.find((k) => k !== nameC && k !== stC && k !== valC && rw0.some((r) => typeof r[k] === 'string' && r[k].trim() && r[k] !== r[nameC])) || '';
    const extra = cols.filter((c) => ![nameC, stC, valC, detC].includes(c)).slice(0, 2);
    const stSrc = srcCol(o, stC), word = (r) => stC ? yesNoWord(r[stC], stSrc) : '';
    const fsel = String(ui(o, 'f', ''));
    let list = fsel ? rw0.filter((r) => word(r) === fsel) : rw0.slice();
    if (dr.sort || ui(o, 'sort', '')) list = tableSort(list, cols, o).rw;
    else if (stC) list = list.map((r, i) => [r, i]).sort((a, b) => stRank(word(a[0])) - stRank(word(b[0])) || a[1] - b[1]).map((x) => x[0]);
    let sum = '';
    if (stC && rw0.length >= 3) { const cnt = {}; rw0.forEach((r) => { const k = word(r); cnt[k] = (cnt[k] || 0) + 1; }); const ks = Object.keys(cnt).sort((a, b) => stRank(a) - stRank(b) || cnt[b] - cnt[a]);
      sum = '<div class="vb-r2s"><div class="bar">' + ks.map((k) => '<i style="flex:' + cnt[k] + ';background:' + stCol(k) + '" title="' + esc(k + ' \u00b7 ' + cnt[k]) + '"></i>').join('') + '</div><div class="lg">'
        + ks.slice(0, 6).map((k) => '<button' + set('f', fsel === k ? '' : k) + ' class="' + (fsel === k ? 'on' : '') + '" title="show only ' + esc(k) + '"><i style="background:' + stCol(k) + '"></i>' + esc(k || '(none)') + '<b>' + cnt[k] + '</b></button>').join('') + (fsel ? '<button' + set('f', '') + '>all</button>' : '') + '</div></div>'; }
    let hasDet = !!(detC || extra.length), rowH = (hasDet ? 31 : 23) * tk, sumH = sum ? 40 * tk : 0;
    /* the summary earns its room: a list of one status says nothing with it, and in a slot too small for four rows under it
       the rows are what matter (a composite's list showed one row and '+ 7 more') */
    if (sum && ((new Set(rw0.map(word))).size < 2 || (o && o.height && (o.height - sumH) / rowH < 4))) { sum = ''; sumH = 0; }
    const lim = dr.limit ? Math.max(1, Math.min(+dr.limit, o && o.height ? Math.max(1, Math.floor((o.height - sumH - 16 * tk) / rowH)) : +dr.limit)) : (o && o.height ? Math.max(2, Math.floor((o.height - sumH - 16 * tk) / rowH)) : (TABLE_ROWS[size] || 4));
    const shown = list.slice(0, lim), more = list.length - shown.length;
    const hi = valC ? Math.max(0, ...list.map((r) => typeof r[valC] === 'number' ? Math.abs(r[valC]) : 0)) || 1 : 1;
    const unit = dr.unit ? ' ' + dr.unit : '', bytes = !!dr.bytes, pillOK = !Wd || Wd >= 300;
    const txtOf = (c, v) => v == null || v === '' ? '' : typeof v === 'number' ? (bytes && c === valC ? fmtBytesS(v) : fmt(v) + (c === valC ? unit : '')) : typeof v === 'boolean' ? yesNoWord(v, srcCol(o, c)) : isoTime(v) ? ago(v) : String(v);
    const body = shown.map((r) => { const w = word(r), v = valC ? r[valC] : undefined;
      const bar = (valC && typeof v === 'number') ? '<i class="bar" style="width:' + Math.max(2, Math.min(100, Math.abs(v) / hi * 100)).toFixed(0) + '%"></i>' : '';
      const dv = detC ? r[detC] : '', ex = extra.map((c) => { const t = txtOf(c, r[c]); return t && typeof r[c] !== 'object' ? '<em>' + esc(c.replace(/_/g, ' ')) + ' ' + esc(t.slice(0, 40)) + '</em>' : ''; }).join('');
      return '<div class="vb-dgr" data-r2' + itemAttr(r, 'row') + '>' + (stC ? '<i class="st" style="background:' + stCol(w) + '"></i>' : '<i class="st none"></i>')
        + '<span class="nm"><b title="' + esc(String(r[nameC] ?? '')) + '">' + esc(String(r[nameC] ?? '')) + '</b>' + (dv || ex ? '<small>' + (dv ? esc(isoTime(dv) ? ago(dv) : String(dv).slice(0, 160)) : '') + ex + '</small>' : '') + '</span>'
        + (stC && pillOK && w ? '<span class="pill" style="--c:' + stCol(w) + '">' + esc(w) + '</span>' : '')
        + (valC ? '<span class="vv">' + bar + '<span>' + esc(txtOf(valC, v)) + '</span></span>' : '') + '</div>'; }).join('');
    const rest = list.slice(lim, lim + 200), moreEl = more > 0 ? '<span class="vb-lbl vb-r2m"' + itemAttr(rest, 'the rest') + ' title="open the rest in the drawer">+ ' + more + ' more</span>' : '';
    return wrap('rows', sum + body + moreEl, 'vb-tf vb-r2w');
  };
  R.cards = (d, H, o) => {
    const rw = rows(d); if (!rw.length) return EMPTY('cards need rows'); const size = (o && o.size) || 'm'; const ncol = { s: 1, m: 2, l: 3, xl: 4 }[size] || 2; const lim = (o && o.draw && o.draw.limit) || ncol * (o && o.height ? Math.max(1, Math.floor(o.height / 66)) : (size === 'xl' ? 3 : 2)); const cols = tableCols(rw, o, 5); const nameC = cols.find((c) => /^(name|title|label|id|host|hostname|node)$/i.test(c)) || cols[0]; const rest = cols.filter((c) => c !== nameC && !/^(status|state|health)$/i.test(c)).slice(0, 2); const stC = cols.find((c) => /^(status|state|health)$/i.test(c));
    return wrap('cards', '<div class="vb-cards" style="grid-template-columns:repeat(' + ncol + ',minmax(0,1fr))">' + rw.slice(0, lim).map((r) => '<div' + itemAttr(r, 'card') + ' class="vb-card">' + (stC ? '<i style="background:' + stCol(r[stC]) + '" title="' + esc(String(r[stC] ?? '')) + '"></i>' : '') + '<b>' + esc(String(r[nameC] ?? '')) + '</b>' + rest.map((c) => '<span><small>' + esc(c.replace(/_/g, ' ')) + '</small>' + esc(typeof r[c] === 'number' ? fmt(r[c]) : String(r[c] ?? '')) + '</span>').join('') + '</div>').join('') + '</div>' + (rw.length > lim ? cap('+ ' + (rw.length - lim) + ' more') : ''));
  };
  R.temps = (d, H, o) => {
    let kv = keyed(d); if (!kv.length) return EMPTY('a temp list needs named values'); const size = (o && o.size) || 'm'; const lim = (o && o.draw && o.draw.limit) || TABLE_ROWS[size] || 4;
    const max = num(o && o.draw && o.draw.max) || Math.max(95, ...kv.map((x) => x[1])), thr = num(o && o.draw && o.draw.throttle) || 70, unit = (o && o.draw && o.draw.unit) || '°'; if ((o && o.draw && o.draw.sort) !== 'name') kv = kv.slice().sort((a, b) => b[1] - a[1]);
    return wrap('temps', kv.slice(0, lim).map((x) => { const col = x[1] >= thr ? B.ac4 : x[1] >= thr * .78 ? B.ac3 : B.ac2; return '<span class="vb-rw"' + itemAttr({ name: x[0], value: x[1], throttle: thr }, 'reading') + '><span class="n" title="' + esc(x[0]) + '">' + esc(x[0]) + '</span><span class="tr"><i style="width:' + pct(x[1], max).toFixed(1) + '%;background:' + col + '"></i><em style="left:' + pct(thr, max).toFixed(1) + '%"></em></span><span class="v" style="color:' + col + '">' + esc(fmt(x[1]) + unit) + '</span></span>'; }).join('') + (kv.length > lim ? '<span class="vb-lbl">+ ' + (kv.length - lim) + ' more · throttle ' + thr + esc(unit) + '</span>' : cap('throttle ' + thr + esc(unit) + ' · hottest first')), 'vb-tf');
  };
  R.gallery = (d, H, o) => {
    const rw = rows(d); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const items = rw.length ? rw : str.map((s) => ({ src: s, name: s.split('/').pop() })); if (!items.length) return EMPTY('a gallery needs items');
    return wrap('gallery', '<div class="vb-gal" style="height:' + chH(H, 4) + 'px">' + items.slice(0, (o && o.draw && o.draw.thumbs) || 8).map((g, i) => { const src = g.src ?? g.url ?? g.thumb ?? g.image ?? ''; const bg = src ? 'url(' + esc(String(src)) + ') center/cover' : (g.bg || 'linear-gradient(135deg,' + mix(DV(i), 55, B.s3) + ',' + mix(DV(i + 3), 35, B.s3) + ')'); return '<div class="g" style="background:' + bg + '" title="' + esc(nameOf(g)) + '"><b>' + esc(String(g.kind ?? g.ext ?? g.k ?? '')) + '</b><span>' + esc(nameOf(g)) + '</span></div>'; }).join('') + '</div>');
  };
  R.terminal = (d, H, o) => {
    const lines = Array.isArray(d) ? d.map((l) => typeof l === 'string' ? l : txt(l)) : (typeof d === 'string' ? d.split('\n') : (d && typeof d === 'object' ? (Array.isArray(d.lines) ? d.lines.map((l) => typeof l === 'string' ? l : txt(l)) : String(d.text ?? d.output ?? d.tail ?? '').split('\n')) : []));
    if (!lines.length || !lines.some((l) => l)) return EMPTY('a terminal needs lines');
    const head = (d && typeof d === 'object' && !Array.isArray(d)) ? [d.session ?? d.host ?? d.name, d.state ?? (d.attached ? 'attached' : ''), d.when ?? d.t].filter(Boolean).join(' · ') : ((o && o.title) || '');
    // as many lines as the body holds (9.5 px mono at 1.6 is ~15.2 px a line, less the head and the padding) - the
    // Diagnostics tile drew twelve into room for nine and cut the last three mid-line
    const tail = lines.slice(-((o && o.draw && o.draw.lines) || Math.max(3, Math.floor(((H || 96) - (head ? 24 : 0) - 16) / 15.2))));
    const ln = (l) => { const s = esc(l); const m = s.match(/^([^\s:$#]+@[^\s:$#]+:[^$#]*[$#])(.*)$/); if (m) return '<b>' + m[1] + '</b>' + m[2]; return s.replace(/(\b\d+ (?:re-embeds|passed|failed)\b)/g, '<em>$1</em>'); };
    return wrap('terminal', (head ? '<div class="vb-trmh"><i></i><span>' + esc(head) + '</span></div>' : '') + '<div class="vb-trm">' + tail.map(ln).join('\n') + '<span class="car"></span></div>');
  };
  R.agenda = (d, H, o) => {
    const ev = evsOf(d); if (!ev.length) return EMPTY('an agenda needs bookings');
    const nowH = new Date().getHours() * 60 + new Date().getMinutes(); const mins = (r) => { const m = hhmm(r.when ?? r.t ?? r.start ?? r.time).match(/(\d{1,2}):(\d{2})/); return m ? +m[1] * 60 + +m[2] : -1; };
    let cur = ev.findIndex((r) => r.now || r.current || r.cls === 'now'); if (cur < 0) { let best = -1; ev.forEach((r, i) => { const m = mins(r); if (m >= 0 && m <= nowH) best = i; }); cur = best; }
    // as many bookings as the body holds (a booking is ~31 px), from the one that is on now - a two-row tile cut its third in half
    const fit = Math.max(1, Math.min(8, Math.floor((H || 96) / 31))), from = cur > 0 ? Math.min(cur, Math.max(0, ev.length - fit)) : 0;
    return wrap('agenda', ev.slice(from, from + fit).map((r, j, _a, i = j + from) => '<div class="vb-ag' + (i === cur ? ' now' : '') + '"' + itemAttr(r, 'booking') + '><span class="t">' + esc(hhmm(r.when ?? r.t ?? r.start ?? r.time)) + '</span><i style="background:' + (r.col || r.color || (stCol(r.status) === B.t3 ? DV(i) : stCol(r.status))) + '"></i><span class="n">' + esc(String(r.title ?? r.name ?? txt(r))) + (r.detail || r.d || r.who ? '<small>' + esc(String(r.detail ?? r.d ?? r.who)) + '</small>' : '') + '</span><span class="w">' + esc(String(r.duration ?? r.w ?? (i === cur ? 'now' : ''))) + '</span></div>').join(''));
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
    const tr = trendOf(d, o); const unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : '');
    const dl = l.delta != null ? '<span class="vb-lbl ' + (num(l.delta) >= 0 ? 'up' : 'dn') + '">' + (num(l.delta) >= 0 ? '▲' : '▼') + ' ' + esc(fmt(Math.abs(num(l.delta)))) + '</span>' : '';
    const chart = tr.length > 1 ? '<div class="vb-chart" style="height:' + chH(H, 62) + 'px"><svg viewBox="0 0 150 80" preserveAspectRatio="none"><path d="M0,80 L' + poly(tr, 150, 80, 5).join(' L') + ' L150,80 Z" fill="' + B.ac + '" fill-opacity=".16"/><polyline points="' + poly(tr, 150, 80, 5).join(' ') + '" fill="none" stroke="' + B.ac + '" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg></div>' : '';
    const sorted = tr.slice().sort((a, b) => a - b); const note = tr.length > 1 ? 'peak ' + fmt(sorted[sorted.length - 1]) + ' · median ' + fmt(sorted[Math.floor(sorted.length / 2)]) : '';
    return wrap('hero', '<div class="vb-hero"><b>' + esc(fmt(l.v)) + '</b><span class="u">' + esc(unit) + '</span>' + dl + '</div>' + chart + cap(note));
  };
  R.gauge = (d, H, o) => {
    const l = level(d); const isLevel = !!(d && typeof d === 'object' && !Array.isArray(d) && typeof d.value === 'number');   // { value, min, max, unit } is one gauge, not value · min · max
    const kv = l && (isLevel || !keyed(d).length) ? [[(o && o.title) || 'value', l.v, l.hi, l.unit]] : keyed(d).map((x) => [x[0], x[1], (o && o.draw && o.draw.max) || 100, '']); if (!kv.length) return EMPTY('gauges need values');
    const arc = Math.PI * 24, pal = palOf(o, 'load'); const rw = rows(d); const meta = (k) => rw.find((r) => nameOf(r) === k) || {};
    return wrap('gauge', '<div class="vb-row">' + kv.slice(0, 4).map((g, i) => { const m = meta(g[0]); const hi = num(m.max ?? g[2]) || 100, f = Math.max(0, Math.min(1, g[1] / hi)); const col = pal(i, g[1], hi); return '<span class="vb-gg"><svg width="62" height="40" viewBox="0 0 62 40"><path d="M7 36 A24 24 0 0 1 55 36" fill="none" stroke="' + B.s3 + '" stroke-width="6" stroke-linecap="round"/><path d="M7 36 A24 24 0 0 1 55 36" fill="none" stroke="' + col + '" stroke-width="6" stroke-linecap="round" stroke-dasharray="' + (arc * f).toFixed(1) + ' ' + arc.toFixed(1) + '"/></svg><b style="color:' + col + '">' + esc(String(m.text ?? (fmt(g[1]) + (m.unit ?? g[3] ?? '')))) + '</b><span class="vb-lbl">' + esc(g[0]) + '</span></span>'; }).join('') + '</div>');
  };
  R.ring = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a ring needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1))), C = 2 * Math.PI * 26, unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : ''), big = Math.min(96, Math.max(44, H - 20));   // the dial and its caption inside the body (H + 14 overflowed a two-row tile)
    const note = (d && d.note) || (unit === '%' ? '' : fmt(l.v) + ' of ' + fmt(l.hi) + (unit ? ' ' + unit : ''));
    return wrap('ring', '<span class="vb-dial" style="width:' + big + 'px;height:' + big + 'px"><svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="26" fill="none" stroke="' + B.s3 + '" stroke-width="7"/><circle cx="32" cy="32" r="26" fill="none" stroke="' + palOf(o, 'accent')(0, f * 100, 100) + '" stroke-width="7" stroke-linecap="round" stroke-dasharray="' + (C * f).toFixed(1) + ' ' + C.toFixed(1) + '"/></svg><span>' + Math.round(f * 100) + '%</span></span>' + cap(esc(note)), 'vb-center');
  };
  R.meter = (d, H, o) => {
    const l = level(d); if (!l) return EMPTY('a meter needs { value, min, max }');
    const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1))), unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : ''); const col = (d && d.col) || ((o && o.draw && o.draw.palette) ? palOf(o)(0, f * 100, 100) : B.ac); const tr = trendOf(d, o);
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
    return wrap('level', '<div class="vb-batt"><div class="cells">' + Array.from({ length: cells }, (_, i) => '<i class="' + (i < held ? (i >= held - warn ? 'on warn' : 'on') : '') + '" data-tip="' + esc((i < held ? 'in use' : 'free') + '\n' + fmt(l.v) + ' of ' + fmt(l.hi) + ' ' + (l.unit || (d && d.what) || 'held') + ' · ' + fmt(l.hi - l.v) + ' free') + '"></i>').join('') + '</div><b></b></div><div class="vb-hero"><b>' + esc(fmt(l.v)) + '</b><span class="u">/ ' + esc(fmt(l.hi)) + ' ' + esc(l.unit || (d && d.what) || 'held') + '</span></div>' + ((H || 96) >= 100 || (d && d.note) ? cap(esc(String((d && d.note) || (fmt(l.hi - l.v) + ' free')))) : ''));   // "n free" only where there is room: the figure already says n of max
  };

  /* ── series: the stacked area, the histogram, the step chart, slope, horizon, bump, small multiples, the spark table ── */
  R.area = (d, H, o) => {
    const ms = multi(d); if (!ms.length || !ms.some((s) => s.v.length > 1)) return EMPTY('an area needs series');
    const W = 300, N = Math.max(...ms.map((s) => s.v.length)); const base = new Array(N).fill(H); const tot = new Array(N).fill(0); ms.forEach((s) => s.v.forEach((v, i) => { tot[i] += v; })); const hi = Math.max(...tot) || 1; const sp0 = serPal(o, ms.map((s) => s.n)), pal = (i, a, b, n) => sp0(i, n);
    const aItem = (s) => { const v = s.v; return { name: s.n, last: v[v.length - 1], min: Math.min(...v), max: Math.max(...v), points: v.length }; };
    const paths = ms.map((s, si) => { const top = base.map((b, i) => b - (s.v[i] || 0) / hi * (H - 6)); const dd = 'M0,' + base[0].toFixed(1) + ' ' + top.map((y, i) => 'L' + ((i / (N - 1)) * W).toFixed(1) + ',' + y.toFixed(1)).join(' ') + ' L' + W + ',' + base[N - 1].toFixed(1) + ' Z'; for (let i = 0; i < N; i++) base[i] = top[i]; return '<path data-b="ar' + si + '"' + itemAttr(aItem(s), 'series') + ' data-tip="' + esc(s.n + '\nlatest ' + fmt(s.v[s.v.length - 1])) + '" d="' + dd + '" fill="' + (s.col || pal(si, 0, 0, s.n)) + '" fill-opacity=".55"/>'; });   // palette status colours a series by its name (pass green, fail red)
    return wrap('area', '<div class="vb-chart" style="height:' + chH(H, 26) + 'px"><svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' + paths.join('') + '</svg></div><div class="vb-lg row">' + ms.map((s, i) => '<span data-b="ar' + i + '"' + itemAttr(aItem(s), 'series') + '><i style="background:' + (s.col || pal(i, 0, 0, s.n)) + '"></i>' + esc(s.n) + (s.v.length ? '<b>' + esc(fmt(s.v[s.v.length - 1])) + '</b>' : '') + '</span>').join('') + '</div>');
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
    /* each series its own colour - its name's dot is the key - where one accent for all said nothing about which is which;
       a series that timed out or is down takes its status colour; each row carries its series (latest · min · max) */
    const sp = serPal(o, ms.map((s) => s.n)), unit = (o && o.draw && o.draw.unit) || '';
    return wrap('small-multiples', ms.slice(0, (o && o.draw && o.draw.rows) || 6).map((s, i) => { const last = s.r && (s.r.last ?? s.r.value ?? s.r.v); const bad = s.r && (s.r.status === 'timeout' || s.r.state === 'down' || s.r.timeout); const col = bad ? stCol(s.r.status || s.r.state || 'timeout') : sp(i, s.n, s.col);
      const vv = s.v.filter((x) => isFinite(x)), st = vv.length ? { last: vv[vv.length - 1], min: Math.min(...vv), max: Math.max(...vv) } : {};
      const fig = bad ? String(s.r.status || 'timeout') : (last != null ? String(typeof last === 'number' ? fmt(last) : last) : fmt(s.v[s.v.length - 1]));
      const it = Object.assign({ name: s.n }, st, unit ? { unit } : {}, { points: s.v.length }, s.r && s.r.status ? { status: s.r.status } : {});
      return '<span class="vb-sm" data-b="sm' + i + '"' + itemAttr(it, 'series') + ' data-tip="' + esc(s.n + '\nlatest ' + fig + (unit ? ' ' + unit : '') + (vv.length ? '\nmin ' + fmt(st.min) + ' · max ' + fmt(st.max) : '')) + '"><span class="vb-lbl"><i class="vb-smk" style="background:' + col + '"></i>' + esc(s.n) + '</span>' + spark(s.v, col) + '<span class="v" style="color:' + col + '">' + esc(fig) + '</span></span>'; }).join(''));
  };
  R['spark-table'] = (d, H, o) => {
    const rw = rows(d); const ms = multi(d); if (!ms.length || !ms.some((s) => s.r)) return EMPTY('a spark table needs rows with a series');
    const extra = Object.keys(ms[0].r || {}).filter((k) => !Array.isArray(ms[0].r[k]) && typeof ms[0].r[k] !== 'object' && !['name', 'title', 'label', 'id', 'col', 'color', 'status', 'state'].includes(k)).slice(0, 3);
    return wrap('spark-table', '<div class="vb-sth"><span>' + esc((o && o.draw && o.draw.name) || 'row') + '</span><span>' + esc((o && o.draw && o.draw.figure) || 'trend') + '</span>' + extra.map((k) => '<span>' + esc(k) + '</span>').join('') + '</div>' + ms.slice(0, 8).map((s, i) => { const bad = s.r && /timeout|down/.test(String(s.r.status ?? s.r.state ?? '')); return '<div class="vb-str" style="grid-template-columns:50px 1fr' + ' 40px'.repeat(extra.length) + '"><span class="n">' + esc(s.n) + '</span>' + spark(s.v, bad ? B.ac3 : (s.col || B.ac)) + extra.map((k) => '<span class="v">' + esc(String(s.r[k] ?? '—')) + '</span>').join('') + '</div>'; }).join(''));
  };

  /* ── MULTI-LINE (owner, 2026-09-28: "more multi-line charts that are color coded" · "better color coding and keys on all
     widgets that would make sense on"). Several series on one set of axes, each its own colour, and a KEY that names every
     series with its latest value. The colours: a record's draw.colors {name: colour} first, then its draw.palette, else -
     when every series is named with a status word (pass · fail, ok · down) - the status colours, else the categorical set
     (--b-dv1 … 7) in order. One y scale shared by every series with two or three gridlines and their values; a time axis
     (first · middle · last) when the points carry times (epoch seconds or ms, ISO, the hour buckets 2026-09-25T08); x is
     then the time, so series sampled at different moments still line up. Hover a column: every series' value at that x
     (the part carries them as its item, so a click opens the drawer on that moment); hover a line or its key entry: that
     series lit, the others dimmed; a click opens the drawer on the series (name, latest, min, max, mean, points).
     It reads:
       {name: [numbers | {t, v}]}              what read.map.split makes, a fleet keyed by node ({series: {…}} too)
       [{t, a, b, c}, …]                        rows: draw.series ['a', 'b'] names the fields (else every numeric field
                                                that is not a time or an id; a row with v / value is one series)
       [{name, points | series | values: […]}] one entry per series
       [{t, v, via}, …] with draw.by 'via'      rows grouped into one series per value of that field (draw.blank names
                                                the rows where it is empty)
       [n, n, n]                                one series
     draw: series · by · blank · value (the field a grouped row's value is) · names {key: label} · colors · palette ·
     min · max · unit · limit (series, default 8). frame.motion false (or draw.motion false) draws it still. */
  const lnTime = (x) => {
    if (x == null || x === '' || typeof x === 'boolean') return null;
    if (typeof x === 'number') return x > 1e12 ? x : (x > 1e9 ? x * 1000 : null);
    const s = String(x).trim(); if (/^\d+(\.\d+)?$/.test(s)) return lnTime(+s);
    const h = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2})$/); const ms = Date.parse(h ? h[1] + 'T' + h[2] + ':00:00Z' : s);
    return isFinite(ms) ? ms : null;
  };
  const LN_MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const lnP2 = (n) => String(n).padStart(2, '0');
  /* a time as the axis says it: the clock under a day and a half, the day (and hour) past it; the hover card's head is fuller */
  const lnLabel = (ms, span, full) => { const t = new Date(ms); if (!isFinite(t.getTime())) return '';
    const hm = lnP2(t.getHours()) + ':' + lnP2(t.getMinutes()), day = t.getDate() + ' ' + LN_MON[t.getMonth()];
    if (full) return (span >= 20 * 3600e3 ? day + ' ' : '') + hm + (span < 600e3 ? ':' + lnP2(t.getSeconds()) : '');
    return span >= 36 * 3600e3 ? day + (span < 6 * 86400e3 ? ' ' + lnP2(t.getHours()) + 'h' : '') : hm + (span < 600e3 ? ':' + lnP2(t.getSeconds()) : ''); };
  const lnNice = (x) => { if (!(x > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(x))), f = x / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; };
  const lnTick = (v) => { const a = Math.abs(v); return a >= 1e6 ? (Math.round(v / 1e5) / 10) + 'M' : a >= 1e4 ? (Math.round(v / 100) / 10) + 'k' : fmt(v); };
  /* the colour of each series: its own, the record's colors, its palette, the status words when every name is one, else dv1…7 */
  const serPal = (o, names) => { const dr = (o && o.draw) || {}; const colors = (dr.colors && typeof dr.colors === 'object') ? dr.colors : {};
    const allSt = names.length > 0 && names.every((n) => stCol(n) !== B.t3); const pal = dr.palette ? palOf(o) : null;
    return (i, n, own) => own || colors[n] || (pal ? pal(i, 0, 0, n) : (allSt ? stCol(n) : DV(i))); };
  /* the series a lines chart draws: [{n, pts: [{t (ms | null), v, i}], col, r, abs}] */
  function linesOf(d, o) {
    const dr = (o && o.draw) || {};
    const val = (x) => (x == null || x === '' || (typeof x === 'string' && !isFinite(+x))) ? null : num(x);
    const pt = (p, i, vk) => { if (p == null || typeof p !== 'object') return { t: null, v: val(p), i };
      const t0 = p.t ?? p.ts ?? p.time ?? p.hour ?? p.when ?? p.at; return { t: lnTime(t0), v: val(vk ? pick(p, vk) : (p.v ?? p.value ?? p.y ?? p.close)), i }; };
    let src = d, out = [];
    if (src && typeof src === 'object' && !Array.isArray(src) && src.series && typeof src.series === 'object') src = src.series;
    if (Array.isArray(src)) {
      const rw = src.filter((r) => r && typeof r === 'object' && !Array.isArray(r));
      const listOf = (r) => ['points', 'series', 'values', 'history', 'spark'].map((k) => r[k]).find((a) => Array.isArray(a));
      if (src.length && Array.isArray(src[0])) out = src.map((s, i) => ({ n: 's' + (i + 1), pts: s.map((p, j) => pt(p, j)) }));
      else if (rw.length && rw.some((r) => listOf(r))) out = rw.filter((r) => listOf(r)).map((r, i) => ({ n: nameOf(r) || 's' + (i + 1), pts: listOf(r).map((p, j) => pt(p, j)), col: r.col || r.color, r }));
      else if (rw.length && dr.by) { const by = {}, order = [];
        rw.forEach((r, j) => { const k0 = pick(r, dr.by); const k = (k0 == null || k0 === '') ? String(dr.blank || '(none)') : String(k0); if (!by[k]) { by[k] = []; order.push(k); } by[k].push(pt(r, j, dr.value)); });
        out = order.map((k) => ({ n: k, pts: by[k], abs: rw.length })); }
      else if (rw.length) { let fs = Array.isArray(dr.series) && dr.series.length ? dr.series.map(String) : null;
        if (!fs && !rw.some((r) => r.v != null || r.value != null || r.y != null)) fs = Object.keys(rw[0]).filter((k) => typeof rw[0][k] === 'number' && !/^(t|ts|time|x|i|id|idx|index|hour|when|at|_score)$/.test(k)).slice(0, 6);
        out = (fs && fs.length) ? fs.map((f) => ({ n: f, pts: rw.map((r, j) => pt(r, j, f)) })) : [{ n: String(dr.name || ''), pts: rw.map((r, j) => pt(r, j)) }]; }
      else if (src.length && src.every((x) => typeof x === 'number' || (typeof x === 'string' && x.trim() !== '' && isFinite(+x)))) out = [{ n: String(dr.name || ''), pts: src.map((x, j) => ({ t: null, v: num(x), i: j })) }];
    } else if (src && typeof src === 'object') {
      out = Object.keys(src).filter((k) => Array.isArray(src[k]) && src[k].length && k !== 'nodes' && k !== 'links').map((k) => ({ n: k, pts: src[k].map((p, j) => pt(p, j)) }));
    }
    const names = (dr.names && typeof dr.names === 'object') ? dr.names : {};
    return out.map((s) => Object.assign(s, { n: String(names[s.n] ?? s.n), pts: s.pts.filter((p) => p.v != null).slice(-240) })).filter((s) => s.pts.length).slice(0, Math.max(1, +dr.limit || 8));
  }
  const lnStats = (s) => { const v = s.pts.map((p) => p.v); return { last: v[v.length - 1], min: Math.min(...v), max: Math.max(...v), mean: Math.round(v.reduce((a, b) => a + b, 0) / v.length * 100) / 100 }; };
  R.lines = (d, H, o) => {
    const ms = linesOf(d, o); if (!ms.length || !ms.some((s) => s.pts.length > 1)) return EMPTY('a line chart needs a series of two points or more');
    const dr = (o && o.draw) || {}, unit = String(dr.unit || ''), k = Math.max(1, (o && o.textK) || 1), Wd = (o && o.width) || 300;
    const pal = serPal(o, ms.map((s) => s.n)); ms.forEach((s, i) => { s.c = pal(i, s.n, s.col); s.st = lnStats(s); });
    const still = !!((o && o.record && o.record.frame && o.record.frame.motion === false) || dr.motion === false);
    const big = (o && (o.size === 'l' || o.size === 'xl')) || Wd >= 600;
    /* x: the time when every point has one, else the place in its series (a shorter series ends at the right edge) */
    const timed = ms.every((s) => s.pts.every((p) => p.t != null)); const allT = timed ? ms.flatMap((s) => s.pts.map((p) => p.t)) : [];
    const t0 = timed ? Math.min(...allT) : 0, t1 = timed ? Math.max(...allT) : 0, useT = timed && t1 > t0, span = t1 - t0;
    const N = Math.max(...ms.map((s) => s.abs || s.pts.length));
    const xOf = (s, p, j) => useT ? (p.t - t0) / span * 1000 : (N > 1 ? ((s.abs ? p.i : (N - s.pts.length + j)) / (N - 1)) * 1000 : 500);
    /* y: one scale for all - from 0 when nothing is negative, to 100 for a percent that stays under it, gridlines at a nice step */
    const vs = ms.flatMap((s) => s.pts.map((p) => p.v)); let lo = Math.min(...vs), hi = Math.max(...vs);
    const fixLo = dr.min != null && dr.min !== '', fixHi = dr.max != null && dr.max !== '';
    if (fixLo) lo = num(dr.min); else if (lo >= 0) lo = 0;
    if (fixHi) hi = num(dr.max); else if (unit === '%' && hi <= 100 && lo >= 0) hi = 100;
    if (!(hi > lo)) hi = lo + 1;
    let step = lnNice((hi - lo) / 2); if (!fixLo) lo = Math.floor(lo / step) * step; if (!fixHi) hi = Math.ceil(hi / step - 1e-9) * step;
    if ((hi - lo) / step > 4) { step *= 2; if (!fixHi) hi = Math.ceil(hi / step - 1e-9) * step; }
    const yOf = (v) => 100 - (v - lo) / ((hi - lo) || 1) * 100;
    const ticks = []; for (let v = lo, n = 0; v <= hi + step * 1e-6 && n < 6; v += step, n++) ticks.push(Math.round(v * 1e6) / 1e6);
    const tl = (v, top) => lnTick(v) + (unit === '%' ? '%' : (top && unit ? ' ' + unit : ''));
    /* the key: every series, its colour, its latest value (and its peak when there is room); what does not fit is counted */
    const kw = (s) => (22 + (s.n.length + tl(s.st.last).length + (big ? 9 : 0)) * 6) * k;
    let kx = 0, krow = 1, kn = 0; const krows = big ? 3 : 2;
    for (const s of ms) { const w = kw(s); if (kx && kx + w > Wd) { krow++; kx = 0; } if (krow > krows) break; kx += w + 10 * k; kn++; }
    if (kn < ms.length) kn = Math.max(1, kn - 1);
    const sItem = (s) => Object.assign({ name: s.n }, s.st, unit ? { unit } : {}, { points: s.pts.length }, useT ? { from: lnLabel(s.pts[0].t, span, true), to: lnLabel(s.pts[s.pts.length - 1].t, span, true) } : {});
    const sTip = (s) => s.n + '\nlatest ' + tl(s.st.last, true) + '\nmin ' + tl(s.st.min, true) + ' · max ' + tl(s.st.max, true);
    const one = ms.length === 1 && !ms[0].n;
    const key = one ? '' : '<div class="vb-lnk">' + ms.slice(0, kn).map((s, i) => '<span data-b="ln' + i + '"' + itemAttr(sItem(s), 'series') + ' data-tip="' + esc(sTip(s)) + '"><i style="background:' + s.c + '"></i><em>' + esc(s.n) + '</em><b>' + esc(tl(s.st.last, true)) + '</b>' + (big ? '<small>peak ' + esc(tl(s.st.max)) + '</small>' : '') + '</span>').join('')
      + (kn < ms.length ? '<span class="more" data-tip="' + esc(ms.slice(kn).map((s) => s.n + ' · ' + tl(s.st.last, true)).join('\n')) + '">+ ' + (ms.length - kn) + '</span>' : '') + '</div>';
    const keyRows = one ? 0 : Math.min(krow, krows), xH = useT ? 13 * k : 0;
    const plotH = Math.max(30, Math.round((H || 96) - keyRows * 15 * k - xH - (keyRows ? 3 : 0) - (xH ? 3 : 0) - 6 * k));
    /* the drawing: gridlines, the series (a lone series with its area), a wide invisible twin of each line to hover, the columns */
    const grid = ticks.map((v) => '<line class="gl" x1="0" x2="1000" y1="' + yOf(v).toFixed(2) + '" y2="' + yOf(v).toFixed(2) + '" vector-effect="non-scaling-stroke"/>').join('');
    const ptsOf = (s) => s.pts.map((p, j) => xOf(s, p, j).toFixed(1) + ',' + yOf(p.v).toFixed(2)).join(' ');
    const area = ms.length === 1 && ms[0].pts.length > 1 ? (() => { const s = ms[0], a = s.pts.map((p, j) => [xOf(s, p, j), yOf(p.v)]); return '<path d="M' + a[0][0].toFixed(1) + ',100 L' + a.map((q) => q[0].toFixed(1) + ',' + q[1].toFixed(2)).join(' L') + ' L' + a[a.length - 1][0].toFixed(1) + ',100 Z" fill="' + s.c + '" fill-opacity=".12"/>'; })() : '';
    /* the columns: up to 48 (or one per point), each carrying every series' value nearest its middle */
    /* the columns sit on the moments the longest series was sampled at (else on the places), at most 48, each as wide as
       half-way to its neighbours; every series gives the value it has nearest the column's middle, inside the column */
    let cx; if (useT) { const ref = ms.reduce((a, s) => (s.pts.length > a.pts.length ? s : a)); cx = ref.pts.map((p) => (p.t - t0) / span * 1000); }
    else cx = Array.from({ length: Math.max(1, N) }, (_, c) => N > 1 ? c / (N - 1) * 1000 : 500);
    if (cx.length > 48) { const st = (cx.length - 1) / 47; cx = Array.from({ length: 48 }, (_, c) => cx[Math.round(c * st)]); }
    const cols = []; for (let c = 0; c < cx.length; c++) {
      const xm = cx[c], x0 = c ? (cx[c - 1] + xm) / 2 : 0, x1 = c < cx.length - 1 ? (xm + cx[c + 1]) / 2 : 1000, tol = Math.max(x1 - x0, 2);
      const at = {}, lines = []; let near = null;
      ms.forEach((s) => { let best = null, bd = Infinity; s.pts.forEach((p, j) => { const dx = Math.abs(xOf(s, p, j) - xm); if (dx < bd) { bd = dx; best = p; } });
        if (best && bd <= tol) { at[s.n || 'value'] = best.v; lines.push((s.n || 'value') + ' · ' + tl(best.v, true)); if (near == null) near = best; } });
      if (!lines.length) continue;
      const pIdx = Math.round(xm / 1000 * Math.max(0, N - 1)) + 1;
      const head = useT && near && near.t != null ? lnLabel(near.t, span, true) : 'point ' + pIdx;
      const it = Object.assign(useT ? { time: head } : { point: pIdx }, at, unit ? { unit } : {});
      cols.push('<g class="hx"' + itemAttr(it, 'point') + ' data-tip="' + esc(head + '\n' + lines.join('\n')) + '"><rect x="' + x0.toFixed(1) + '" y="0" width="' + Math.max(1, x1 - x0).toFixed(1) + '" height="100"/><line x1="' + xm.toFixed(1) + '" x2="' + xm.toFixed(1) + '" y1="0" y2="100" vector-effect="non-scaling-stroke"/></g>');
    }
    const paths = ms.map((s, i) => { const pp = ptsOf(s), it = itemAttr(sItem(s), 'series'), tp = ' data-tip="' + esc(sTip(s)) + '"';
      return '<polyline class="ln" data-b="ln' + i + '"' + it + tp + ' points="' + pp + '" stroke="' + s.c + '" vector-effect="non-scaling-stroke"/><polyline class="lnh" data-b="ln' + i + '"' + it + tp + ' points="' + pp + '" vector-effect="non-scaling-stroke"/>'; }).join('');
    const ends = ms.map((s) => { const p = s.pts[s.pts.length - 1], j = s.pts.length - 1; return '<i class="end" style="left:' + (xOf(s, p, j) / 10).toFixed(2) + '%;top:' + yOf(p.v).toFixed(2) + '%;background:' + s.c + '"></i>'; }).join('');
    const yw = Math.round(Math.max(22, Math.max(...ticks.map((v, i) => tl(v, i === ticks.length - 1).length)) * 5.8 + 4) * k);
    const yax = '<div class="vb-lny" style="width:' + yw + 'px">' + ticks.map((v, i) => '<span style="top:' + yOf(v).toFixed(2) + '%">' + esc(tl(v, i === ticks.length - 1)) + '</span>').join('') + '</div>';
    const xl = useT ? (Wd < 260 ? [0, 1] : [0, .5, 1]).map((f) => '<span style="left:' + (f * 100) + '%;transform:translateX(' + (f === 0 ? '0' : f === 1 ? '-100%' : '-50%') + ')">' + esc(lnLabel(t0 + f * span, span)) + '</span>').join('') : '';
    return wrap('lines', key + '<div class="vb-lnp" style="height:' + plotH + 'px">' + yax + '<div class="vb-lnc"><svg viewBox="0 0 1000 100" preserveAspectRatio="none">' + grid + area + cols.join('') + paths + '</svg>' + ends + '</div></div>'
      + (useT ? '<div class="vb-lnx"><span class="pad" style="width:' + yw + 'px"></span><div>' + xl + '</div></div>' : ''), still ? 'still' : '');
  };
  /* the status key a list of statuses draws under it: each status present, its colour and how many (two or more kinds) */
  const stKey = (sts) => { const c = {}, order = []; sts.forEach((s) => { const k = String(s == null ? '' : s); if (!k) return; if (!(k in c)) { c[k] = 0; order.push(k); } c[k]++; });
    return order.length > 1 ? '<div class="vb-stkey">' + order.slice(0, 6).map((k) => '<span' + itemAttr({ status: k, count: c[k] }, 'status') + ' data-tip="' + esc(k + '\n' + c[k] + ' of ' + sts.length) + '"><i style="background:' + stCol(k) + '"></i>' + esc(k) + '<b>' + c[k] + '</b></span>').join('') + '</div>' : ''; };
  const LINES_CSS = `
/* the multi-line chart and the keys (2026-09-28) */
.vb-lines{gap:3px}
.vb-lnk{display:flex;flex-wrap:wrap;gap:2px 10px;font-size:10px;line-height:1.35;min-width:0;flex:none}
.vb-lnk span{display:inline-flex;align-items:center;gap:5px;min-width:0;max-width:100%;white-space:nowrap;cursor:pointer;color:var(--b-t2)}
.vb-lnk i{width:11px;height:3px;border-radius:2px;flex:none}
.vb-lnk em{font-style:normal;overflow:hidden;text-overflow:ellipsis}
.vb-lnk b{font-family:var(--b-mono);font-weight:600;color:var(--b-t1)}
.vb-lnk small{font-family:var(--b-mono);font-size:9px;color:var(--b-t3)}
.vb-lnk span.more{color:var(--b-t3)}
.vb-lnp{display:flex;gap:5px;width:100%;min-height:30px;flex:none;margin-top:6px}
.vb-lny{position:relative;flex:none;font-family:var(--b-mono);font-size:9px;color:var(--b-t3)}
.vb-lny span{position:absolute;right:0;transform:translateY(-50%);white-space:nowrap;line-height:1}
.vb-lnc{position:relative;flex:1;min-width:0;height:100%}
.vb-lnc svg{display:block;width:100%;height:100%;overflow:visible}
.vb-lnc .gl{stroke:var(--b-bd2);stroke-width:1;stroke-dasharray:2 3}
.vb-lnc .ln{fill:none;stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round}
.vb-lnc .lnh{fill:none;stroke:transparent;stroke-width:10;pointer-events:stroke}
.vb-lnc .hx rect{fill:transparent}.vb-lnc .hx line{stroke:var(--b-t2);stroke-width:1;opacity:0}
.vb-lnc .hx.hot{filter:none}.vb-lnc .hx.hot line{opacity:.6}.vb-lnc .hx.hot rect{fill:color-mix(in srgb,var(--b-t1) 6%,transparent)}
.vb-lnc .end{position:absolute;width:6px;height:6px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 0 2px var(--b-surf);pointer-events:none}
.vb-lines:has([data-b].hot) .ln:not(.hot){opacity:.25}.vb-lines .ln.hot{stroke-width:2.8;filter:none}
.vb-lines:has(.vb-lnk span.hot) .end{opacity:.35}
.vb-lnx{display:flex;gap:5px;height:12px;flex:none;font-family:var(--b-mono);font-size:9px;color:var(--b-t3)}
.vb-lnx .pad{flex:none}.vb-lnx div{position:relative;flex:1;min-width:0}.vb-lnx div span{position:absolute;top:0;white-space:nowrap}
:host(:not([data-entered])) .vb-lines:not(.still) .ln{animation:vb-lnwipe .9s cubic-bezier(.2,.7,.2,1) both}
@keyframes vb-lnwipe{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
.vb-lines.still *{animation:none!important;transition:none!important}
/* keys on the other forms: the series dot of a small multiple, the status key under a list, the heat map's scale */
.vb-sm{grid-template-columns:62px 1fr 44px}.vb-sm .vb-lbl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vb-sm .vb-lbl .vb-smk{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:4px;vertical-align:1px}
.vb-stkey{display:flex;flex-wrap:wrap;gap:2px 10px;font-size:9.5px;color:var(--b-t2);flex:none}
.vb-stkey span{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}.vb-stkey i{width:7px;height:7px;border-radius:50%}.vb-stkey b{font-family:var(--b-mono);font-weight:400;color:var(--b-t1)}
.vb-heatkey{display:flex;align-items:center;gap:6px;font-family:var(--b-mono);font-size:9px;color:var(--b-t3);flex:none}
.vb-heatkey .g{flex:0 1 90px;height:6px;border-radius:3px}.vb-heatkey span{display:inline-flex;align-items:center;gap:4px}.vb-heatkey span i{width:8px;height:8px;border-radius:2px}
.vb-area .vb-lg b,.vb-stacked-bar .vb-lg b{margin-left:4px}
`;

  /* ── values: columns, ranked, lollipop, waterfall, pareto, box, diverging, bullet, threshold, radar, numbers, pills ── */
  R.column = (d, H, o) => {
    const kv = keyed(d); const vals = kv.length ? kv.map((x) => x[1]) : series(d); if (!vals.length) return EMPTY('columns need values');
    const pal = palOf(o, 'load'), lim = (o && o.draw && o.draw.limit) || 24, from = Math.max(0, vals.length - lim), hi = Math.max(...vals.slice(from)) || 1;   /* the drawn window's peak: the whole history's hid every bar under one old spike */
    // named columns say the first and the last name they span (dreams per day: 2026-06-28 → 2026-07-19); a bar's own title
    // is its own name (it was the name of the bar lim places earlier once there were more than lim)
    const span = kv.length > 1 ? esc(String(kv[from][0])) + ' → ' + esc(String(kv[kv.length - 1][0])) + ' · ' : '';
    return wrap('column', '<div class="vb-chart" style="height:' + chH(H, 22) + 'px"><div class="vb-colbars gap">' + vals.slice(from).map((v, i) => '<i' + itemAttr(kv[from + i] ? { name: kv[from + i][0], value: v } : { index: from + i, value: v }, 'bar') + ' style="height:' + pct(v, hi).toFixed(0) + '%;background:' + pal(i, v, hi) + '" title="' + esc(kv[from + i] ? String(kv[from + i][0]) + ' · ' : '') + fmt(v) + '"></i>').join('') + '</div></div>' + cap((d && d.note) ? esc(String(d.note)) : span + 'last ' + Math.min(vals.length, lim) + ' · peak ' + esc(fmt(hi))));
  };
  // the rows a bar list holds in its body (a bar row is ~13 px with the 6 px gap) - six rows in room for four cut two off
  const barRowsFit = (H) => Math.max(2, Math.floor(((H || 96) + 6) / 19));
  // labels that all begin the same way ("jaahas/qwen3.5-uncensored:9b", "jaahas/qwen3.5-uncensored:27b") drop the shared
  // part up to its last separator, so the part that tells them apart is what the narrow label column shows
  const shortLabels = (names) => { if (names.length < 2) return names; let p = names[0]; names.forEach((n) => { while (p && n.indexOf(p) !== 0) p = p.slice(0, -1); });
    const cut = Math.max(p.lastIndexOf('/'), p.lastIndexOf(':'), p.lastIndexOf('.'), p.lastIndexOf('_'), p.lastIndexOf('-')) + 1; return cut >= 4 && names.every((n) => n.length > cut) ? names.map((n) => '…' + n.slice(cut)) : names; };
  const rkShort = (v) => { const n = num(v), a = Math.abs(n), f = (x, u, p) => (Math.round(x * p) / p).toString() + u; return a >= 1e9 ? f(n / 1e9, 'B', 100) : a >= 1e6 ? f(n / 1e6, 'M', 100) : a >= 1e4 ? f(n / 1e3, 'k', 10) : fmt(v); };
  R.ranked = (d, H, o) => {
    const rw = rows(d), stOf = (r) => r.status ?? r.state, sts = rw.map(stOf).filter((s) => s != null && s !== '' && stCol(s) !== B.t3), withKey = new Set(sts.map(String)).size > 1;
    const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, (o && o.draw && o.draw.limit) || Math.max(1, barRowsFit(H) - (withKey ? 1 : 0))); if (!kv.length) return EMPTY('ranked bars need values'); const hi = kv[0][1] || 1, pal = palOf(o);
    /* a bar's colour: its own, the record's palette, its row's status, its name when every name is a status word, else dv in rank order */
    const usePal = !!(o && o.draw && o.draw.palette), allSt = kv.every((x) => stCol(x[0]) !== B.t3);
    const rCol = (r, x, i) => r.col || (usePal ? pal(i, x[1], hi, stOf(r) ?? x[0]) : (sts.length && stOf(r) != null && stCol(stOf(r)) !== B.t3 ? stCol(stOf(r)) : (allSt ? stCol(x[0]) : pal(i))));
    const lbl = shortLabels(kv.map((x) => x[0]));
    return wrap('ranked', kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; return '<span class="vb-rw"' + itemAttr(Object.keys(r).length ? r : { name: x[0], value: x[1] }, 'bar') + '><span class="n" title="' + esc(x[0]) + '">' + esc(lbl[i]) + '</span><span class="tr"><i style="width:' + pct(x[1], hi).toFixed(1) + '%;background:' + rCol(r, x, i) + '"></i></span><span class="v">' + esc(String(r.text ?? r.size ?? rkShort(x[1]))) + '</span></span>'; }).join('') + (withKey ? stKey(sts) : ''));
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
    const kv = keyed(d).slice(0, Math.min(6, (o && o.draw && +o.draw.limit) || 6, barRowsFit(H))); if (!kv.length) return EMPTY('bullet bars need values'); const rw = rows(d); const hi = Math.max(...kv.map((x) => x[1]), ...rw.map((r) => num(r.max ?? r.target ?? 0))) || 1, pal = palOf(o, 'status');
    return wrap('bullet', kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; const tg = r.target ?? (o && o.draw && o.draw.target); return '<span class="vb-rw"' + itemAttr(Object.keys(r).length ? r : { name: x[0], value: x[1] }, 'bar') + '><span class="n">' + esc(x[0]) + '</span><span class="tr"><i style="width:' + pct(x[1], num(r.max) || hi).toFixed(1) + '%;background:' + (r.col || (r.status ? stCol(r.status) : DV(i))) + '"></i>' + (tg != null ? '<em style="left:' + pct(num(tg), num(r.max) || hi).toFixed(1) + '%"></em>' : '') + '</span><span class="v">' + esc(String(r.text ?? fmt(x[1]))) + '</span></span>'; }).join(''));
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
  const fmtBytes = (v) => { let n = Math.abs(num(v)); const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? (n >= 100 ? Math.round(n) : Math.round(n * 10) / 10) : Math.round(n)) + ' ' + u[i]; };
  R.numbers = (d, H, o) => {
    const kv = keyed(d).slice(0, 6); const rw = rows(d); if (!kv.length) return EMPTY('a number grid needs values'); const pal = palOf(o, 'accent');
    const bytes = new Set((o && o.draw && Array.isArray(o.draw.bytes)) ? o.draw.bytes.map(String) : []);   // the figures that are byte counts
    // the figures in one row when the body is wide enough for them (four in a 4-wide tile), else two a row, smaller - two
    // rows of 30 px figures and their labels were taller than a two-row tile's body
    const perRow = (o && o.width && o.width / kv.length >= 95) ? kv.length : Math.min(2, kv.length), nRows = Math.ceil(kv.length / perRow);
    /* a figure fits its cell: its size follows the cell (container units), and a count of a million or more in a row of three
       or more is said short (1.06M) - the whole number stays in the item, the hover card and the drawer (2026-09-27) */
    const shortN = (v) => { const n = num(v), a = Math.abs(n), f = (x, u) => (Math.round(x * 100) / 100).toString() + u; return a >= 1e9 ? f(n / 1e9, 'B') : a >= 1e6 ? f(n / 1e6, 'M') : fmt(n); };
    const fig = (v) => (perRow >= 3 && Math.abs(num(v)) >= 1e6) ? shortN(v) : fmt(v);
    return wrap('numbers', '<div class="vb-bigs' + (nRows > 1 ? ' tworow' : '') + '" style="grid-template-columns:repeat(' + perRow + ',1fr)">' + kv.map((x, i) => { const r = rw.find((q) => nameOf(q) === x[0]) || {}; return '<div' + itemAttr(Object.keys(r).length ? r : (bytes.has(x[0]) ? { name: x[0], value: fmtBytes(x[1]), unit: 'bytes' } : { name: x[0], value: x[1] }), 'figure') + '><b style="color:' + (r.col || (i === 0 ? B.ac : i === 1 ? B.ac2 : i === 2 ? B.ac3 : B.t1)) + '">' + esc(String(r.text ?? (bytes.has(x[0]) ? fmtBytes(x[1]) : fig(x[1])))) + '</b><span>' + esc(x[0]) + '</span></div>'; }).join('') + '</div>');
  };
  // how many pills of these labels the body holds (a pill is 22 px tall with a 5 px gap; ~5.6 px a character at 9.5 px,
  // plus its padding, dot and figure): the rest become one "+ N" pill, never a third row cut in half
  const pillsFit = (labels, H, W, k) => { k = k || 1; const rowsN = Math.max(1, Math.floor(((H || 96) + 5) / (27 * k))), w = Math.max(120, W || 300); let row = 0, x = 0, n = 0;
    for (let i = 0; i < labels.length; i++) { const pw = Math.min(w, 32 * k + labels[i].length * 5.6 * k); if (x && x + pw > w) { row++; x = 0; } if (row >= rowsN) break; x += pw + 5 * k; n++; }
    return n >= labels.length ? n : Math.max(1, n - 1); };
  const pillMore = (k, st) => k > 0 ? '<span class="more" title="' + esc(st) + '">+ ' + k + '</span>' : '';
  R.pills = (d, H, o) => {
    const kv = keyed(d); const st = rows(d); const W = o && o.width;
    if (st.length && st.some((r) => r.status != null || r.state != null)) { const sts = st.map((r) => String(r.status ?? r.state ?? '')).filter(Boolean), withKey = new Set(sts).size > 1 && (H || 96) >= 56; const n = pillsFit(st.map((r) => nameOf(r)), withKey ? (H || 96) - 16 * ((o && o.textK) || 1) : H, W, o && o.textK); return wrap('pills', '<div class="vb-pillw">' + st.slice(0, n).map((r) => { const s = String(r.status ?? r.state ?? ''); return '<span' + itemAttr(r, 'pill') + ' title="' + esc(nameOf(r) + ' · ' + s) + '"><i style="background:' + stCol(s) + '"></i>' + esc(nameOf(r)) + '</span>'; }).join('') + pillMore(st.length - n, st.slice(n).map((r) => nameOf(r)).join(', ')) + '</div>' + (withKey ? stKey(sts) : '')); }
    if (kv.length) { const n = pillsFit(kv.map((x) => x[0] + ' ' + fmt(x[1])), H, W, o && o.textK); return wrap('pills', '<div class="vb-pillw">' + kv.slice(0, n).map((x, i) => '<span' + itemAttr({ name: x[0], value: x[1] }, 'pill') + '><i style="background:' + DV(i) + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '</b></span>').join('') + pillMore(kv.length - n, kv.slice(n).map((x) => x[0]).join(', ')) + '</div>'); }
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
    const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a stacked bar needs parts'); const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1; const rw = rows(d);
    const sp = serPal(o, kv.map((x) => x[0])), rOf = (x) => rw.find((q) => nameOf(q) === x[0]) || {}, pal = (i) => sp(i, kv[i][0], rOf(kv[i]).col), sh = (x) => Math.round(Math.abs(x[1]) / tot * 1000) / 10;
    const it = (x) => itemAttr(Object.assign({}, rOf(x), { name: x[0], value: x[1], share: sh(x), of: tot }), 'part');
    return wrap('stacked-bar', '<span class="vb-stackbar">' + kv.map((x, i) => '<i data-b="sb' + i + '"' + it(x) + ' style="width:' + (Math.abs(x[1]) / tot * 100).toFixed(1) + '%;background:' + pal(i) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + ' · ' + sh(x) + '%"></i>').join('') + '</span><div class="vb-lg">' + kv.map((x, i) => { const r = rOf(x); return '<span data-b="sb' + i + '"' + it(x) + '><i style="background:' + pal(i) + '"></i>' + esc(x[0]) + '<b>' + esc(String(r.text ?? fmt(x[1]))) + '</b></span>'; }).join('') + '</div>');
  };
  R.treemap = (d, H, o) => {
    const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, 10); if (!kv.length) return EMPTY('a treemap needs parts'); const sp = serPal(o, kv.map((x) => x[0])), pal = (i) => sp(i, kv[i][0]);
    const tmTot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1, tmSh = (x) => Math.round(Math.abs(x[1]) / tmTot * 1000) / 10;
    // the board's rule for its eight: two big, two middling, the rest small — widths within a row by share
    // the rows share the whole height between them (three rows 45 · 28 · 24; one or two stretched to fill it, not left short)
    const rowsOf = [kv.slice(0, 2), kv.slice(2, 4), kv.slice(4)].filter((r) => r.length), hs0 = [45, 28, 24].slice(0, rowsOf.length), hsum = hs0.reduce((a, b) => a + b, 0), hs = hs0.map((h) => Math.floor(h * 97 / hsum));
    return wrap('treemap', '<div class="vb-tmap" style="height:' + chH(H, 4) + 'px">' + rowsOf.map((r, ri) => { const t = r.reduce((s, x) => s + x[1], 0) || 1; return r.map((x) => '<span' + itemAttr({ name: x[0], value: x[1], share: tmSh(x), of: tmTot }, 'part') + ' style="width:calc(' + (x[1] / t * 100).toFixed(1) + '% - 2px);height:' + hs[ri] + '%;background:' + pal(kv.indexOf(x)) + '" title="' + esc(x[0]) + ' · ' + fmt(x[1]) + ' · ' + tmSh(x) + '%">' + esc(x[0]) + '</span>').join(''); }).join('') + '</div>');
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
      // the cells share the body's height (square cells three to a row were 130 px tall and ran off the tile), the columns are
      // named over the grid, a cell wide enough says its value; palette load colours a cell by its own value (a percent
      // against 100); draw.total false drops the row sums (cpu + ram + disk adds up to nothing)
      const nr = Math.min(8, m.rows.length), hd = m.cols.length ? 14 : 0, kh = (!(o && o.draw && o.draw.key === false) && (H || 96) >= 64) ? 14 : 0, cellH = Math.max(8, Math.min(26, Math.floor(((H || 96) - hd - kh) / nr) - 4));
      const load = o && o.draw && o.draw.palette === 'load', lpal = palOf(o, 'load'), tot = !(o && o.draw && o.draw.total === false), wide = !o || !o.width || o.width / Math.max(1, m.cols.length || m.rows[0].v.length) >= 60;
      const cols = m.cols.length ? m.cols.length : m.rows[0].v.length, grid = 'grid-template-columns:repeat(' + cols + ',1fr)';
      const head = hd ? '<span class="vb-heatrow hd"><span></span><span class="vb-heat" style="' + grid + '">' + m.cols.map((c) => '<b title="' + esc(String(c)) + '">' + esc(String(c)) + '</b>').join('') + '</span>' + (tot ? '<span></span>' : '') + '</span>' : '';
      return wrap('heat', head + m.rows.slice(0, nr).map((r, ri) => '<span class="vb-heatrow' + (tot ? '' : ' nt') + '"' + itemAttr(m.cols.length ? r.v.reduce((o3, x, ci) => { o3[m.cols[ci] || ('c' + ci)] = x; return o3; }, { name: r.n }) : { name: r.n, values: r.v }, 'row') + '><span class="vb-lbl" title="' + esc(r.n) + '">' + esc(r.n) + '</span><span class="vb-heat" style="' + grid + '">' + r.v.map((x, ci) => '<i style="height:' + cellH + 'px;aspect-ratio:auto;background:' + (load ? mix(lpal(ci, num(x), hi <= 100 ? 100 : hi), 80, 'transparent') : mix(pal(ri), Math.round(8 + num(x) / hi * 88), 'transparent')) + '" title="' + esc(r.n) + (m.cols[ci] ? ' · ' + esc(String(m.cols[ci])) : '') + ' · ' + fmt(x) + '">' + (wide && cellH >= 14 ? esc(fmt(x)) : '') + '</i>').join('') + '</span>' + (tot ? '<span class="v">' + esc(fmt(r.t != null ? r.t : r.v.reduce((s, x) => s + num(x), 0))) + '</span>' : '') + '</span>').join('') + (kh ? (() => { if (load) { const bd = (o.draw && Array.isArray(o.draw.bands) && o.draw.bands.length) ? o.draw.bands.map(num) : [60, 85]; return '<div class="vb-heatkey"><span><i style="background:' + mix(B.ac2, 80) + '"></i>&lt; ' + fmt(bd[0]) + '</span><span><i style="background:' + mix(B.ac3, 80) + '"></i>' + fmt(bd[0]) + '–' + fmt(bd[1]) + '</span><span><i style="background:' + mix(B.ac4, 80) + '"></i>≥ ' + fmt(bd[1]) + '</span></div>'; }
        return '<div class="vb-heatkey"><span>0</span><span class="g" style="background:linear-gradient(90deg,' + mix(B.t2, 10) + ',' + mix(B.t2, 96) + ')"></span><span>' + esc(fmt(hi)) + '</span></div>'; })() : ''), 'vb-heatfit'); }
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
    // as many lines as the body holds (a line is ~15 px: 9 px type at 1.55 + the gap) - sixteen lines in a two-row tile were
    // squashed onto each other - and the NEWEST of them, whichever end of the list the source keeps its newest at
    const n = (o && o.draw && (o.draw.limit || o.draw.tail)) || Math.max(3, Math.floor(H / (15 * ((o && o.textK) || 1))));
    const tOf = (r) => { const v = r.t ?? r.ts ?? r.time ?? r.when; const x = typeof v === 'number' ? v : Date.parse(String(v || '')); return isFinite(x) ? x : NaN; };
    const newestFirst = rw.length > 1 && tOf(rw[0]) > tOf(rw[rw.length - 1]);
    return wrap('log', '<div class="vb-log">' + (newestFirst ? rw.slice(0, n) : rw.slice(-n)).map((r) => { const k = String(r.kind ?? r.level ?? r.type ?? ''); return '<span' + itemAttr(r, 'line') + ' style="color:' + (r.col || kc(k)) + '"><span class="t">' + esc(hhmm(r.t ?? r.ts ?? r.time ?? r.when)) + '</span> ' + (k ? '<span class="k">' + esc(k) + '</span> ' : '') + esc(txt(r)) + '</span>'; }).join('') + '</div>');
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
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a graph needs nodes'); const pos = (o && o.draw && o.draw.layout === 'flow') ? layers(g) : layers(g); const W = (o && o.width) ? Math.max(160, o.width) : 300, hi = Math.max(...g.links.map((e) => e.v), 1); const pal = palOf(o);
    return wrap('graph', '<div class="vb-topo" style="height:' + Math.max(80, H) + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, Math.max(80, H)); return '<span class="te" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';transform:rotate(' + s.deg + ')"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id]; const dd = 8 + Math.min(10, num(n.size ?? n.weight ?? n.score * 12 ?? 4)); return '<span class="tn" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;width:' + dd + 'px;height:' + dd + 'px;background:' + (n.col || n.color || (n.kind || n.family ? DV(String(n.kind || n.family).length) : (i ? pal(i) : B.ac))) + '" title="' + esc(String(n.label ?? n.name ?? n.id)) + '"></span>'; }).join('') + '</div>', '', 'width:' + ((o && o.width) ? '100%' : W + 'px') + ';max-width:100%');
  };
  R.flow = (d, H, o) => {
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a flow needs nodes and flows'); const hi = Math.max(...g.links.map((e) => e.v), 1); const W = (o && o.width) ? Math.max(160, o.width) : 300, HH = Math.max(80, H); const pos = {}; const lay = layers(g); Object.keys(lay).forEach((k) => { pos[k] = [lay[k][1], lay[k][0]]; });   // the flow runs left → right: depth is x
    return wrap('flow', '<div class="vb-topo" style="height:' + HH + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, HH); return '<span class="fe" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';height:' + (2 + e.v / hi * 6).toFixed(1) + 'px;transform:rotate(' + s.deg + ')" title="' + esc(e.a + ' → ' + e.b + (e.label ? ' · ' + e.label : '')) + '"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id]; return '<span class="fn" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;color:' + (n.col || n.color || (i ? DV(i) : B.ac)) + '">' + esc(String(n.label ?? n.name ?? n.id)) + '</span>'; }).join('') + '</div>', '', 'width:' + ((o && o.width) ? '100%' : W + 'px') + ';max-width:100%');
  };
  R.minigraph = (d, H, o) => {
    const g = graphOf(d); if (!g.nodes.length) return EMPTY('a mini graph needs nodes'); const pos = layers(g); const W = (o && o.width) ? Math.max(120, o.width) : 220, HH = Math.max(70, H);
    return wrap('minigraph', '<div class="vb-topo" style="height:' + HH + 'px">' + g.links.map((e) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return ''; const s = edgeSpan(a, b, W, HH); return '<span class="te" style="left:' + a[0].toFixed(1) + '%;top:' + a[1].toFixed(1) + '%;width:' + s.len + ';transform:rotate(' + s.deg + ')"></span>'; }).join('') + g.nodes.map((n, i) => { const p = pos[n.id], hollow = n.included === false || n.hollow; const dd = 6 + Math.min(8, num(n.score != null ? n.score * 8 : (n.size ?? 3))); return '<span class="tn' + (hollow ? ' hollow' : '') + '" style="left:' + p[0].toFixed(1) + '%;top:' + p[1].toFixed(1) + '%;width:' + dd + 'px;height:' + dd + 'px;--c:' + (n.col || n.color || DV(String(n.family ?? n.kind ?? i).length + i)) + '" title="' + esc(String(n.label ?? n.name ?? n.id)) + '"></span>'; }).join('') + '</div>', '', 'width:' + ((o && o.width) ? '100%' : W + 'px') + ';max-width:100%');
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
    if (pg === 0) body = '<div class="vb-carp"><span class="vb-dial"><svg viewBox="0 0 64 64">' + '<circle cx="32" cy="32" r="26" fill="none" stroke="' + B.s3 + '" stroke-width="9"/>' + parts.map((p) => { const fr = p[1] / tot; const el = '<circle cx="32" cy="32" r="26" fill="none" stroke="' + stCol(p[0]) + '" stroke-width="9" stroke-dasharray="' + Math.max(0, C * fr - 2).toFixed(1) + ' ' + (C - C * fr + 2).toFixed(1) + '" stroke-dashoffset="' + (-C * acc).toFixed(1) + '"/>'; acc += fr; return el; }).join('') + '</svg><span>' + tot + '</span></span><div class="vb-lg" style="width:96px">' + parts.map((p) => '<span><i style="background:' + stCol(p[0]) + '"></i>' + esc(p[0]) + '<b>' + p[1] + '</b></span>').join('') + '</div>' + list(rw.slice(0, 4)) + '</div>';
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
    return wrap('rings', '<div class="vb-row"><div class="vb-dials"><svg viewBox="0 0 80 80">' + kv.map((x, i) => { const m = rw.find((q) => nameOf(q) === x[0]) || {}; const f = Math.max(0, Math.min(1, x[1] / (num(m.max) || 100))); return '<circle cx="40" cy="40" r="' + R0[i] + '" fill="none" stroke="' + B.s3 + '" stroke-width="7"/><circle cx="40" cy="40" r="' + R0[i] + '" fill="none" stroke="' + cols[i] + '" stroke-width="7" stroke-dasharray="' + ringD(R0[i], f) + '" stroke-linecap="round"/>'; }).join('') + '</svg></div><div class="vb-lg">' + kv.map((x, i) => '<span><i style="background:' + cols[i] + '"></i>' + esc(x[0]) + '<b>' + esc(fmt(x[1])) + '%</b></span>').join('') + '</div></div>');
  };
  /* ── GLOBE (the Globes board): godseye on the canvas. One globe form, any config: the record's points are pins on an
     orthographic globe that turns to face the set it shows — a FEW at a time, the ones the page asks for, with the list
     beside them and a way to page to the next few (draw.pins, default 4; the page is the widget's own UI state). Links
     are dashed arcs moving at their rate, the night side a terminator for the hour (draw.night · d.night), a sweep turns
     about the centre (draw.sweep). Pins carry a size (cores, load), a colour, a detail and a meta line; open/lit pins
     pulse. The graticule is drawn by projection — no map data is needed and none is faked. ── */
  const GLOBE_R = Math.PI / 180;
  const globePts = (d) => { const src = Array.isArray(d) ? d : (d && typeof d === 'object' ? (d.points || d.pins || d.items || d.hosts || d.rows || d.events || d.nodes || []) : []);
    return (Array.isArray(src) ? src : []).map((p, i) => { if (Array.isArray(p)) return { name: String(p[0] ?? 'pin ' + (i + 1)), lon: num(p[1]), lat: num(p[2]), col: p[3] || '', meta: p[4] || '', detail: p[5] || '' };
      if (!p || typeof p !== 'object') return null; const lon = p.lon ?? p.lng ?? p.longitude ?? (p.geo && (p.geo.lon ?? p.geo.lng)), lat = p.lat ?? p.latitude ?? (p.geo && p.geo.lat); if (lon == null || lat == null || !isFinite(+lon) || !isFinite(+lat)) return null;
      return { name: nameOf(p) || 'pin ' + (i + 1), lon: +lon, lat: +lat, col: p.col || p.color || '', size: num(p.size ?? p.r ?? p.cores ?? p.weight ?? 0), meta: String(p.meta ?? p.m ?? p.source ?? p.when ?? p.t ?? ''), detail: String(p.detail ?? p.d ?? p.text ?? p.summary ?? p.status ?? ''), lit: !!(p.lit ?? p.open ?? p.active ?? (p.status && /open|ok|run|serv/i.test(String(p.status)))), sev: String(p.sev ?? p.severity ?? p.level ?? '') }; }).filter(Boolean); };
  R.globe = (d, H, o) => {
    const pts = globePts(d); if (!pts.length) return EMPTY('a globe needs points with a longitude and a latitude');
    const D = (d && typeof d === 'object' && !Array.isArray(d)) ? d : {}, DR = (o && o.draw) || {}, ui = (o && o.ui) || {}, pal = palOf(o);
    const W = wof(o), HH = hof(H), small = HH < 100, per = Math.max(1, Math.min(12, num(DR.pins ?? D.pins ?? (small ? 2 : 4)) || 4));
    const pages = Math.max(1, Math.ceil(pts.length / per)), page = ((num(ui.gpage) || 0) % pages + pages) % pages, set = pts.slice(page * per, page * per + per);
    // the view faces the middle of the set (the board's centre()), or the record's own view
    const centre = (list) => { let x = 0, y = 0, z = 0; list.forEach((p) => { const l = p.lon * GLOBE_R, f = p.lat * GLOBE_R; x += Math.cos(f) * Math.cos(l); y += Math.cos(f) * Math.sin(l); z += Math.sin(f); }); return [Math.atan2(y, x) / GLOBE_R, Math.atan2(z, Math.sqrt(x * x + y * y)) / GLOBE_R]; };
    const vw = Array.isArray(DR.view) && DR.view.length === 2 ? DR.view : (Array.isArray(D.view) && D.view.length === 2 ? D.view : centre(set)); const lon0 = +vw[0] || 0, lat0 = Math.max(-40, Math.min(40, +vw[1] || 0)), Rr = 100;
    const proj = (lon, lat) => { const l = (lon - lon0) * GLOBE_R, p = lat * GLOBE_R, p0 = lat0 * GLOBE_R; const cosc = Math.sin(p0) * Math.sin(p) + Math.cos(p0) * Math.cos(p) * Math.cos(l);
      return { x: Rr * Math.cos(p) * Math.sin(l), y: -Rr * (Math.cos(p0) * Math.sin(p) - Math.sin(p0) * Math.cos(p) * Math.cos(l)), vis: cosc > 0.02 }; };
    const f1 = (v) => v.toFixed(1);
    const grat = (step) => { const out = []; const run = (walk) => { const seg = []; walk((lon, lat) => { const q = proj(lon, lat); if (q.vis) seg.push(f1(q.x) + ',' + f1(q.y)); else if (seg.length) { out.push(seg.join(' ')); seg.length = 0; } }); if (seg.length > 1) out.push(seg.join(' ')); };
      for (let lon = -180; lon < 180; lon += step) run((f) => { for (let lat = -90; lat <= 90; lat += 5) f(lon, lat); }); for (let lat = -60; lat <= 60; lat += step) run((f) => { for (let lon = -180; lon <= 180; lon += 5) f(lon, lat); }); return out; };
    const night = (sunLon) => { const pts2 = []; for (let i = 0; i <= 72; i++) { const a = i * 5 * GLOBE_R; const lat = Math.asin(Math.sin(a) * 0.98) / GLOBE_R, lon = sunLon + 180 + Math.cos(a) * 90; const q = proj(lon, lat); if (q.vis) pts2.push(f1(q.x) + ',' + f1(q.y)); } return pts2.length < 3 ? '' : 'M' + pts2.join(' L') + ' Z'; };
    const arc = (a, b) => { const out = []; for (let i = 0; i <= 24; i++) { const t = i / 24; const q = proj(a.lon + (b.lon - a.lon) * t, a.lat + (b.lat - a.lat) * t + Math.sin(t * Math.PI) * 8); if (q.vis) out.push(f1(q.x) + ',' + f1(q.y)); } return out.join(' '); };
    const sunLon = (DR.night === true || D.night === true) ? (12 - (new Date().getUTCHours() + new Date().getUTCMinutes() / 60)) * 15 : (typeof (DR.night ?? D.night) === 'number' ? +(DR.night ?? D.night) : null);
    const links = (Array.isArray(D.links) ? D.links : Array.isArray(D.arcs) ? D.arcs : []).map((l) => { const at = (v) => typeof v === 'number' ? pts[v] : pts.find((p) => p.name === String(v)); return Array.isArray(l) ? { a: at(l[0]), b: at(l[1]), col: l[2] } : { a: at(l.from ?? l.source ?? l.a), b: at(l.to ?? l.target ?? l.b), col: l.col || l.color }; }).filter((l) => l.a && l.b);
    const smax = Math.max.apply(null, pts.map((p) => p.size || 0).concat([1]));
    const pin = (p, i) => { const q = proj(p.lon, p.lat); if (!q.vis) return ''; const col = p.col || pal(i % 8), r = p.size ? 3 + (p.size / smax) * 4 : 4, numbered = per <= 6 && !links.length;
      const flag = o && o.posts; const label = small ? '' : '<text class="pl" x="' + f1(q.x + r + 3) + '" y="' + f1(q.y + 3) + '">' + esc(p.name.split(' · ')[0].slice(0, 22)) + '</text>';
      if (flag) return '<line x1="' + f1(q.x) + '" y1="' + f1(q.y) + '" x2="' + f1(q.x) + '" y2="' + f1(q.y - 34) + '" stroke="' + col + '" stroke-width="1.2"/><rect x="' + f1(q.x) + '" y="' + f1(q.y - 45) + '" width="34" height="11" rx="2" fill="' + col + '"/><text class="pn" x="' + f1(q.x + 4) + '" y="' + f1(q.y - 37) + '">' + esc((p.sev || p.name).slice(0, 6)) + '</text><circle class="pin' + (p.lit ? ' p' : '') + '" cx="' + f1(q.x) + '" cy="' + f1(q.y) + '" r="3.5" fill="' + col + '"/>';
      return (p.lit ? '<circle cx="' + f1(q.x) + '" cy="' + f1(q.y) + '" r="' + f1(r * 3.5) + '" fill="' + col + '" fill-opacity=".18"/>' : '') + '<circle class="pin' + (p.lit ? ' p' : '') + '" cx="' + f1(q.x) + '" cy="' + f1(q.y) + '" r="' + f1(r) + '" fill="' + col + '"/>' + (numbered ? '<text class="pn" x="' + f1(q.x) + '" y="' + f1(q.y + 2.6) + '" text-anchor="middle">' + (i + 1) + '</text>' : '') + label; };
    const sweep = (DR.sweep ?? D.sweep) ? '<g class="scan"><circle r="100" fill="none" stroke="none"/><path class="sw" d="M0 0L-25.9 -96.6A100 100 0 0 1 0 -100Z" opacity=".4"/><path class="sw" d="M0 0L-50 -86.6A100 100 0 0 1 -25.9 -96.6Z" opacity=".22"/><path class="sw" d="M0 0L-70.7 -70.7A100 100 0 0 1 -50 -86.6Z" opacity=".1"/><line x1="0" y1="0" x2="0" y2="-100" stroke="' + B.ac2 + '" stroke-width="1.2" stroke-linecap="round"/></g>' : '';
    const gid = 'vbsea' + Math.floor(Math.random() * 1e6);
    const svg = '<svg viewBox="-110 ' + (o && o.posts ? '-118 220 235' : '-110 220 220') + '"><defs><radialGradient id="' + gid + '" cx="38%" cy="32%" r="72%"><stop offset="0" stop-color="#1c2a3d"/><stop offset="1" stop-color="#0d1219"/></radialGradient></defs><circle class="disc" r="100" fill="url(#' + gid + ')"/>'
      + grat(small ? 45 : (num(DR.grat) || 30)).map((s) => '<polyline class="gl" points="' + s + '"/>').join('') + (sunLon != null && night(sunLon) ? '<path class="term" d="' + night(sunLon) + '"/>' : '') + sweep
      + links.map((l, i) => { const p = arc(l.a, l.b); return p ? '<polyline class="arc" points="' + p + '" stroke="' + (l.col || pal(i % 8)) + '"/>' : ''; }).join('') + '<circle class="rim" r="100"/>' + set.map((p, i) => pin(p, i)).join('') + '</svg>';
    const listOn = !small && (DR.list ?? D.list) !== false && W >= 220;
    const list = listOn ? '<div class="side">' + set.map((p, i) => '<span class="r"><i style="background:' + (p.col || pal(i % 8)) + '">' + (per <= 6 && !links.length ? (i + 1) : '') + '</i><span><b>' + esc(p.name.slice(0, 36)) + '</b>' + esc(String(p.detail || '').slice(0, 60)) + (p.meta ? '<br><small>' + esc(String(p.meta).slice(0, 40)) + '</small>' : '') + '</span></span>').join('')
      + (pages > 1 ? '<span class="nav"><button type="button" data-vb-set="gpage:' + ((page + pages - 1) % pages) + '" title="the previous ' + per + '">\u2039</button><button type="button" data-vb-set="gpage:' + ((page + 1) % pages) + '" title="the next ' + per + '">\u203a</button><span>' + (page + 1) + ' / ' + pages + ' \u00b7 ' + pts.length + '</span></span>' : '<span class="nav"><span>' + pts.length + ' point' + (pts.length === 1 ? '' : 's') + '</span></span>') + '</div>' : '';
    return wrap('globe', '<div class="vb-globe' + (o && o.posts ? ' posts' : '') + '" data-page="' + page + '" data-pages="' + pages + '" data-shown="' + set.length + '"><div class="gw">' + (o && o.posts ? '<span class="plinth"></span>' : '') + svg + '</div>' + list + '</div>', '', 'height:' + HH + 'px');
  };
  // the iso globe: on a plinth, the pins flags on posts (the board's alerts on the desk)
  R['globe@iso'] = (d, H, o) => R.globe(d, H, Object.assign({}, o, { posts: true }));
  R.scatter = (d, H, o) => {
    const pts = rows(d).map((p) => ({ x: num(p.x ?? p.t ?? p[0]), y: num(p.y ?? p.v ?? p.value ?? p[1]), s: num(p.size ?? p.r ?? p.calls ?? 3), k: String(p.kind ?? p.class ?? p.label ?? ''), col: p.col || p.color })); if (pts.length < 2) return EMPTY('a scatter needs points');
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y), ss = pts.map((p) => p.s); const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys), s1 = Math.max(...ss) || 1; const kinds = []; const pal = palOf(o);
    return wrap('scatter', '<div class="vb-chart rel" style="height:' + chH(H, 22) + 'px">' + pts.slice(0, 400).map((p) => { let ki = kinds.indexOf(p.k); if (ki < 0) { kinds.push(p.k); ki = kinds.length - 1; } const dd = 5 + p.s / s1 * 12; return '<span style="position:absolute;transform:translate(-50%,-50%);border-radius:50%;left:' + (6 + (p.x - x0) / ((x1 - x0) || 1) * 88).toFixed(1) + '%;top:' + (94 - (p.y - y0) / ((y1 - y0) || 1) * 84).toFixed(1) + '%;width:' + dd.toFixed(1) + 'px;height:' + dd.toFixed(1) + 'px;background:' + (p.col || pal(ki)) + ';opacity:.8" title="' + esc(p.k) + ' ' + fmt(p.x) + ' · ' + fmt(p.y) + '"></span>'; }).join('') + '</div>' + cap(esc(String((d && d.note) || ('x ' + ((o && o.draw && o.draw.x) || 'x') + ' · y ' + ((o && o.draw && o.draw.y) || 'y') + ' · area ' + ((o && o.draw && o.draw.size) || 'size'))))));
  };

  /* ══ THE MOTION AND ISO FORMS — the WidgetsMotion and WidgetsIso boards, on the shared projection ═══════════════════
     Every isometric face is a positioned div with a clip-path from window.VeraISO (/ui/iso.js — the one projection the
     app shares; the element loads it itself and redraws when it arrives). The board's scene builders are here as they
     were written, with the demo constants replaced by the shape's data: a box list, a paint order, a fit to W×H, then
     faces · edges · labels as markup. The motion is the board's CSS (lamps blink, tanks wave, belts move, a sweep turns);
     frame.motion false holds all of it still (data-motion on the root). */
  const ISO = () => (typeof window !== 'undefined' && window.VeraISO) || null;
  const ISO_WAIT = '<div class="vw-b vb-isowait"><span class="vb-lbl">loading the projection…</span></div>';
  /* ── A BLOCK'S DETAIL (the widget review, round 2: "more detail on mouseover of the blocks in all of the iso widgets") ──
     A box drawn for a row carries the row (Bx's last argument, or box.row); the scene numbers every box that has a row
     or a title (iso.js carries a box's n through to its faces), and every face of that box is written with the block's
     id (data-b), its detail (data-tip: the name, then the row's own fields), its entity (data-ref, when the row names
     one the estate knows - guest:<vmid>, host:<id>, mesh:<host>) and its group (data-g: the host a container runs on, the
     rack a guest sits in). The element lights the block and its group on hover, shows the detail, and opens the entity
     or the record's place on a click. */
  const TIP_SKIP = /^(name|title|label|id|key|col|color|cls|ref|u|v|z|w|h|d|t|row|g|_.*)$/;
  const rowRef = (r) => { if (!r || typeof r !== 'object') return ''; const x = r.ref ?? r.entity ?? r.entity_ref; if (x) return String(x);
    if (r.vmid != null && r.vmid !== '') return 'guest:' + r.vmid; if (r.ssh_host_id) return 'host:' + r.ssh_host_id; if (r.host_id && /mesh/i.test(String(r.kind ?? ''))) return 'mesh:' + r.host_id; return ''; };
  const rowGroup = (r) => (r && typeof r === 'object') ? String(r.host ?? r.node ?? r.group ?? r.host_id ?? '') : '';
  const TIP_RAW = /(^|_)(port|id|vmid|pid|db|year|uid|gid)$|^port$/i;   // a number that is a name (a port, an id) is not a quantity: no thousands comma
  const tipVal = (k, v) => Array.isArray(v) ? (v.length && v.length <= 4 && v.every((x) => typeof x !== 'object') ? v.join(', ').slice(0, 64) : v.length + ' ' + (v.length === 1 ? 'item' : 'items')) : (typeof v === 'number' ? (TIP_RAW.test(k) ? String(v) : (/(^|_)(created|updated|started|ended|at|ts|time)$/i.test(k) && v > 1e9 && v < 4e10) ? new Date(v * 1000).toISOString().slice(0, 16).replace('T', ' ') : (/bytes?$|_b$/.test(k) ? fmtBytesS(v) : fmt(v)) + (/_pct$|pct$|percent/.test(k) ? '%' : (/_ms$/.test(k) ? ' ms' : (/_s$/.test(k) ? ' s' : '')))) : (typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v).slice(0, 64)));
  // what a block's detail says first: its state, what it is, where it runs, what it uses - then the rest; hashes and
  // long ids (a container's 64-hex Id, an image digest) say nothing to a reader and are left to the deep dive
  const TIP_FIRST = ['status', 'state', 'Status', 'State', 'health', 'kind', 'type', 'role', 'host', 'node', 'Image', 'image', 'model', 'cpu', 'cpu_pct', 'load', 'mem', 'mem_pct', 'mem_mb', 'ram_pct', 'disk_pct', 'temp', 'max_c', 'value', 'count', 'size', 'port', 'branch', 'owner', 'last_activity', 'when', 'ts'];
  const TIP_HASH = /^(sha256:)?[0-9a-f]{24,}$/i;
  const rowTip = (r, title) => { if (!r || typeof r !== 'object') return String(title || ''); const head = String(title || nameOf(r) || '');
    const ok = (k) => !TIP_SKIP.test(k) && r[k] != null && r[k] !== '' && (typeof r[k] !== 'object' || Array.isArray(r[k])) && !(typeof r[k] === 'string' && (TIP_HASH.test(r[k]) || r[k].length > 90));
    const keys = TIP_FIRST.filter((k) => k in r && ok(k)).concat(Object.keys(r).filter((k) => !TIP_FIRST.includes(k) && ok(k)));
    return [head].concat(keys.slice(0, 9).map((k) => k.replace(/_/g, ' ') + ': ' + tipVal(k, r[k]))).join('\n'); };
  const fmtBytesS = (v) => { let n = Math.abs(num(v)); const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? Math.round(n * 10) / 10 : Math.round(n)) + ' ' + u[i]; };
  const blockAttrs = (b) => b ? ' data-b="' + esc(String(b.id)) + '"' + (b.tip ? ' data-tip="' + esc(b.tip) + '" title="' + esc(b.tip) + '"' : '') + (b.ref ? ' data-ref="' + esc(b.ref) + '"' : '') + (b.g ? ' data-g="' + esc(b.g) + '"' : '') + (b.name ? ' data-name="' + esc(b.name) + '"' : '') + (b.item ? itemAttr(b.item, 'block') : '') : '';
  const fpx = (f) => '<i class="f ' + f.k + (f.cls ? ' ' + f.cls : '') + '" style="left:' + f.x + ';top:' + f.y + ';width:' + f.w + ';height:' + f.h + ';clip-path:' + f.cp + ';background:' + f.col + ';--i:' + f.i + ';--n:' + (f.n || 0) + '"' + (f.info ? blockAttrs(f.info) : (f.t ? ' title="' + esc(f.t) + '"' : '')) + '></i>';
  const epx = (e) => '<span class="isoe' + (e.cls ? ' ' + e.cls : '') + '" style="left:' + e.x + ';top:' + e.y + ';width:' + e.len + ';transform:rotate(' + e.deg + ');background:' + e.col + '"></span>';
  const lpx = (l) => '<span class="isol' + (l.cls ? ' ' + l.cls : '') + '" style="left:' + l.x + ';top:' + l.y + ';color:' + l.col + '">' + esc(l.n) + '</span>';
  const isow = (W, H, inner, style) => '<div class="vb-isow" style="width:' + W + 'px;height:' + H + 'px;' + (style || '') + '">' + inner + '</div>';
  // build a scene and keep the projection + shift, so labels, pings and lines can be placed in the same frame as the faces
  const isoBuild = (boxes, k, W, H, key, tilt, azim, o) => { const I = ISO(); o = o || {};
    const info = {}; let nb = 0;
    boxes = (boxes || []).map((b) => { if (!b || !(b.row || b.t)) return b; const n = ++nb; const nm = b.row ? (nameOf(b.row) || String(b.t || '')) : String(b.t || '');
      info[n] = { id: n, name: nm, tip: b.tip || (b.row ? rowTip(b.row, nm) : String(b.t)), ref: b.ref || (b.row ? rowRef(b.row) : ''), g: b.g != null ? String(b.g) : (b.row ? rowGroup(b.row) : ''), item: b.row || null }; return Object.assign({}, b, { n }); });
    const T = tilt == null ? 30 : tilt, A = azim == null ? 45 : azim;
    const k2 = I.isoFitK(boxes, k, W, H, key, T, A, o); const Pj = I.proj(T, A, k2); const f = I.scene(Pj, boxes, key);
    const sh = I.fit(f, W, H - (o.padb == null ? 18 : o.padb) + (o.padt == null ? 8 : o.padt));
    const at = (u, v, z) => { const p = Pj(u, v, z || 0); return [p[0] + sh.dx, p[1] + sh.dy]; };
    const faces = I.px(f); faces.forEach((fc) => { if (fc.n && info[fc.n]) fc.info = info[fc.n]; });
    return { faces, P: Pj, sh, at, k: k2 }; };
  const lbl = (at, u, v, z, n, col, cls) => { const p = at(u, v, z); return { n, col: col || B.t2, cls: cls || '', x: p[0].toFixed(1) + 'px', y: p[1].toFixed(1) + 'px' }; };
  const segsAt = (at, pts, col, cls) => ISO().segs(pts.map((q) => at(q[0], q[1], q[2]))).map((s) => ({ x: s.x.toFixed(1) + 'px', y: s.y.toFixed(1) + 'px', len: s.len.toFixed(1) + 'px', deg: s.deg.toFixed(1) + 'deg', col, cls: cls || '' }));
  const tcol = (v, hi) => { const f = hi ? v / hi * 100 : v; return f >= 74 ? B.ac4 : f >= 58 ? B.ac3 : B.ac2; };
  const byStack = (b) => (b.u + b.v) * 1000 + b.z;
  const DK = 'var(--b-surf3)';
  const sceneOf = (form, boxes, k, W, H, key, o, extra) => { if (!ISO()) return ISO_WAIT; const b = isoBuild(boxes, k, W, H, key, o && o.tilt, o && o.azim, o); return { b, html: wrap(form, isow(W, H, b.faces.map(fpx).join('') + (extra ? extra(b) : ''))) }; };
  const wof = (o) => Math.max(160, Math.min(640, (o && o.width) || 276));
  const hof = (H, used) => Math.max(72, H - (used || 0));
  const isoOr = (form, d, H, o) => { const r = R[form + '@iso']; return r ? r(d, H, o) : null; };

  /* ── the dial: studs round three quarters of a circle, lit to the value; a hub; the needle to the last lit stud ── */
  const dialScene = (k, W, H, val, o) => { const N = 28, Rr = 2.4, a0 = Math.PI * 0.75, span = Math.PI * 1.5; const lit = Math.round(N * val); const boxes = [];
    for (let i = 0; i < N; i++) { const a = a0 + (i / (N - 1)) * span; const u = Math.cos(a) * Rr, v = Math.sin(a) * Rr; const on = i < lit; const col = !on ? B.s3 : (i < N * .6 ? B.ac2 : i < N * .85 ? B.ac3 : B.ac4); boxes.push({ u: u - .16, v: v - .16, z: 0, w: .32, d: .32, h: on ? .3 : .12, col, n: i, cls: i === lit - 1 ? 'lamp' : '' }); }
    boxes.push({ u: -.35, v: -.35, z: 0, w: .7, d: .7, h: .4, col: DK });
    const b = isoBuild(boxes, k, W, H, null, null, null, o); const an = a0 + (Math.max(1, lit) - 1) / (N - 1) * span;
    return Object.assign(b, { needle: segsAt(b.at, [[0, 0, .42], [Math.cos(an) * Rr * .82, Math.sin(an) * Rr * .82, .32]], B.t1, 'needle') }); };
  R.dial = (d, H, o) => { const l = level(d); if (!l) return EMPTY('a dial needs { value, min, max }'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1)));
    const D1 = dialScene(72, W, HH, f, {}); const unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : '');
    const L = [lbl(D1.at, 0, 1.25, .7, fmt(l.v) + unit, B.t1, 'big dn'), lbl(D1.at, 0, 3.3, 0, String((d && d.note) || (o && o.title) || ''), B.t3, 'dn sm'), lbl(D1.at, -2.5, 2.1, 0, fmt(l.lo), B.t3, 'sm'), lbl(D1.at, 2.5, 2.1, 0, fmt(l.hi), B.t3, 'sm')];
    return wrap('dial', isow(W, HH, D1.faces.map(fpx).join('') + D1.needle.map(epx).join('') + L.map(lpx).join(''))); };
  R['radial@iso'] = (d, H, o) => R.dial(d, H, o);

  /* ── the tank: a level with a moving surface, bubbles, and a feed pipe that runs while something is writing ── */
  const tankScene = (k, W, H, lvl, col, o) => { const HT = 3.6, fill = Math.max(.12, lvl * HT); const boxes = [{ u: -.1, v: -.1, z: -.14, w: 3.4, d: 3.4, h: .14, col: DK, key: 0 }, { u: .08, v: .08, z: 0, w: 3.04, d: 3.04, h: fill, col, cls: 'wave', key: 1 }];
    for (let i = 0; i < 5; i++) boxes.push({ u: .5 + (i % 3) * .9, v: .5 + Math.floor(i / 3) * 1.1, z: fill * .25, w: .16, d: .16, h: .16, col: 'color-mix(in srgb,#fff 55%,transparent)', cls: 'bub', n: i, key: 2 });
    boxes.push({ u: 0, v: 0, z: 0, w: 3.2, d: 3.2, h: HT, col: 'color-mix(in srgb,var(--b-t1) 7%,transparent)', key: 3 });
    return isoBuild(boxes, k, W, H, (b) => b.key * 1000 + b.z, null, null, o); };
  R.tank = (d, H, o) => { const l = level(d); if (!l) return EMPTY('a tank needs { value, max }'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const f = Math.max(0, Math.min(1, (l.v - l.lo) / ((l.hi - l.lo) || 1)));
    const TK = tankScene(66, W, HH, f, (d && d.col) || B.ac, {}); const writing = d && (d.writing || d.rate);
    const E = writing ? segsAt(TK.at, [[-1.9, 1.6, 4.4], [.9, 1.6, 4.4]], mix(B.ac, 55, B.s3), 'flowe').concat(segsAt(TK.at, [[.9, 1.6, 4.4], [.9, 1.6, 3.7]], mix(B.ac, 55, B.s3), 'flowe')) : [];
    const unit = l.unit || ((l.bounded && l.hi === 100 && l.lo === 0) ? '%' : ''); const L = [lbl(TK.at, 1.6, 1.6, 3.15, Math.round(f * 100) + '%', B.t1, 'big'), lbl(TK.at, 1.6, 1.6, 2.55, String((d && d.note) || (fmt(l.v) + (unit === '%' ? '' : ' of ' + fmt(l.hi)) + ' ' + (unit === '%' ? '' : unit))), B.t2, 'sm'), lbl(TK.at, 1.6, 4.1, 0, String((o && o.title) || ''), B.t3, 'dn')].concat(writing ? [lbl(TK.at, -2.2, 1.6, 4.65, 'writing ' + esc(String(d.rate || '')), B.ac2, 'sm')] : []);
    return wrap('tank', isow(W, HH, TK.faces.map(fpx).join('') + E.map(epx).join('') + L.map(lpx).join(''))); };

  /* ── thermometers: a dim tube, a fill to the reading, a glowing cap when hot ── */
  const thermScene = (k, W, H, rws, max, throttle, o) => isoBuild(rws.flatMap((r, i) => { const TB = 3.0, h = r[1] / max * TB, u = i * 1.25, col = r[1] >= throttle ? B.ac4 : r[1] >= throttle * .78 ? B.ac3 : B.ac2;
      const parts = [{ u, v: 0, z: 0, w: .66, d: .66, h: .3, col: DK }, { u: u + .09, v: .09, z: .06, w: .48, d: .48, h: .3, col, cls: r[1] >= throttle ? 'hot' : '' }, { u: u + .15, v: .15, z: .3, w: .36, d: .36, h: TB, col: 'color-mix(in srgb,var(--b-t1) 9%,transparent)' }, { u: u + .15, v: .15, z: .3, w: .36, d: .36, h: Math.max(.06, h), col }, { u: u + .13, v: .13, z: TB + .3, w: .4, d: .4, h: .06, col: B.bd2 }];
      for (let s = 1; s <= 4; s++) parts.push({ u: u + .52, v: .22, z: .3 + (s / 5) * TB, w: .18, d: .06, h: .03, col: s === 4 ? B.ac4 : B.t3 }); return parts; }), k, W, H, (b) => b.u + b.v + b.z, null, null, o || { padt: 16 });
  R['thermo@iso'] = (d, H, o) => { const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('thermometers need values'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const max = num(o && o.draw && o.draw.max) || 95, thr = num(o && o.draw && o.draw.throttle) || 70; const unit = (o && o.draw && o.draw.unit) || '°';
    const TH = thermScene(62, W, HH, kv, max, thr, { padt: 16 }); const L = kv.flatMap((r, i) => [lbl(TH.at, i * 1.25 + .3, 1.05, 0, r[0].split(' ')[0], B.t3, 'dn sm'), lbl(TH.at, i * 1.25 + .3, .3, r[1] / max * 3.0 + .62, fmt(r[1]) + unit, r[1] >= thr ? B.ac4 : r[1] >= thr * .78 ? B.ac3 : B.ac2)]);
    return wrap('thermo', isow(W, HH, TH.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };

  /* ── coin stacks: share as coins, the top one catching the light ── */
  R.stacks = (d, H, o) => { const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('coin stacks need parts'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const cols = (o && o.draw && o.draw.columns) || 4, coin = num(o && o.draw && o.draw.coin) || Math.max(1, Math.max(...kv.map((x) => x[1])) / 8); const pal = palOf(o);
    const CN = isoBuild(kv.flatMap((c, i) => { const n = Math.max(1, Math.round(c[1] / coin)); return Array.from({ length: n }, (_, j) => ({ u: (i % cols) * 1.45, v: Math.floor(i / cols) * 1.55, z: j * .19, w: 1, d: 1, h: .16, col: pal(i), cls: j === n - 1 ? 'shine' : '', n: i })); }), 60, W, HH, (b) => (b.u + b.v) * 100 + b.z);
    const L = kv.map((c, i) => lbl(CN.at, (i % cols) * 1.45 + .5, Math.floor(i / cols) * 1.55 + 1.15, 0, c[0], B.t3, 'dn'));
    return wrap('stacks', isow(W, HH, CN.faces.map(fpx).join('') + L.map(lpx).join(''))); };

  /* ── OHLCV: wick and body as blocks, volume as a row behind ── */
  R['candles@iso'] = (d, H, o) => { const bars = rows(d).filter((r) => r.open != null || r.o != null).slice(-((o && o.draw && o.draw.bars) || 12)); if (bars.length < 2) return EMPTY('candles need OHLC bars'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H);
    const O = (b) => num(b.open ?? b.o), Cc = (b) => num(b.close ?? b.c), Hh = (b) => num(b.high ?? b.h ?? Math.max(O(b), Cc(b))), L = (b) => num(b.low ?? b.l ?? Math.min(O(b), Cc(b))), V = (b) => num(b.volume ?? b.vol ?? b.v ?? 0);
    const lo = Math.min(...bars.map(L)), hi = Math.max(...bars.map(Hh)), sp = (hi - lo) || 1, vmax = Math.max(...bars.map(V)) || 1; const Z = (p) => (p - lo) / sp * 3; const out = [];
    bars.forEach((b, i) => { const up = Cc(b) >= O(b), col = up ? B.ac2 : B.ac4; out.push({ u: i * .8 + .18, v: .18, z: Z(L(b)), w: .14, d: .14, h: Math.max(.05, Z(Hh(b)) - Z(L(b))), col: 'color-mix(in srgb,' + col + ' 60%,var(--b-s3))' }); out.push({ u: i * .8, v: 0, z: Z(Math.min(O(b), Cc(b))), w: .5, d: .5, h: Math.max(.06, Math.abs(Z(Cc(b)) - Z(O(b)))), col }); out.push({ u: i * .8, v: 1.5, z: 0, w: .5, d: .5, h: .1 + V(b) / vmax * 1.1, col: mix(B.ac, 45, B.s3) }); });
    const OB = isoBuild(out, 58, W, HH, (b) => (b.u + b.v) * 100 + b.z); return wrap('candles', isow(W, HH, OB.faces.map(fpx).join('')), 'vb-iso'); };

  /* ── the conveyor: stage platforms on a belt, the work moving along it ── */
  R.conveyor = (d, H, o) => { const st = stagesOf(d); if (!st.length) return EMPTY('a conveyor needs stages'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const n = st.length, work = Math.max(1, Math.min(4, num(o && o.draw && o.draw.work) || 3)); const now = st.findIndex((s) => s.st === 'now');
    const CV = isoBuild([{ u: -.3, v: .35, z: -.05, w: n * 2.1, d: .9, h: .08, col: DK }].concat(st.map((s, i) => ({ u: i * 2.1, v: 0, z: 0, w: 1.6, d: 1.6, h: .28, col: s.st === 'done' ? B.ac2 : s.st === 'now' ? B.ac : s.st === 'bad' ? B.ac4 : B.s3 }))).concat(Array.from({ length: work }, (_, k) => ({ u: .4, v: .5, z: .36, w: .5, d: .5, h: .42, col: B.ac3, cls: 'mv', n: k }))), 58, W, HH, (b) => b.cls === 'mv' ? 1e6 + b.n : (b.u + b.v) * 100 + b.z);
    const a = CV.at(.4, .5, .36), bb = CV.at((n - 1) * 2.1 + .4, .5, .36); const L = st.map((s, i) => lbl(CV.at, i * 2.1 + .8, 1.9, 0, nameOf(s), i === now ? B.t1 : B.t3, 'dn'));
    return wrap('conveyor', isow(W, HH, CV.faces.map(fpx).join('') + L.map(lpx).join(''), '--cx:' + (bb[0] - a[0]).toFixed(1) + 'px;--cy:' + (bb[1] - a[1]).toFixed(1) + 'px')); };

  /* ── the city: footprint is cores, height is load, colour is temperature, a lamp means an alert; roads are the links ── */
  R.city = (d, H, o) => { const g = graphOf(d); const ns = g.nodes.length ? g.nodes : rows(d); if (!ns.length) return EMPTY('a city needs nodes'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H);
    const cols = Math.ceil(Math.sqrt(ns.length)); const N = ns.map((n, i) => ({ id: String(n.id ?? n.name ?? i), cores: num(n.cores ?? n.cpu ?? 4), load: num(n.load ?? n.value ?? 0), temp: num(n.temp ?? n.temperature ?? 0), alert: !!(n.alert || n.down || /down|error/.test(String(n.status ?? ''))), u: n.u != null ? num(n.u) : (i % cols) * 2.4 + .6, v: n.v != null ? num(n.v) : Math.floor(i / cols) * 2.2 + .6, down: !!(n.down || /stopped|down|off/.test(String(n.status ?? ''))) }));
    N.forEach((x, i) => { x.row = ns[i]; });   // each building keeps its row: its hover detail and its deep dive
    const CY = isoBuild(N.flatMap((n) => { const s = .5 + n.cores / 20, h = n.load ? .3 + n.load / 100 * 3 : .18; return [{ u: n.u - s / 2, v: n.v - s / 2, z: 0, w: s, d: s, h, col: n.down ? B.s3 : tcol(n.temp || n.load), row: n.row }].concat(n.alert ? [{ u: n.u - .1, v: n.v - .1, z: h, w: .2, d: .2, h: .22, col: B.ac4, cls: 'lamp' }] : []); }), 40, W, HH, (b) => (b.u + b.v) * 100 + b.z, 32, 42);
    const byId = {}; N.forEach((n) => { byId[n.id] = n; }); const E = g.links.flatMap((e) => { const a = byId[e.a], b = byId[e.b]; if (!a || !b) return []; return segsAt(CY.at, [[a.u, a.v, 0], [b.u, a.v, 0], [b.u, b.v, 0]], mix(B.ac, 40)); });
    const L = N.flatMap((n) => [lbl(CY.at, n.u, n.v + .55, 0, n.id, n.down ? B.ac4 : B.t2, 'dn sm')].concat(n.down ? [lbl(CY.at, n.u + .3, n.v - .3, .5, 'down', B.ac4, 'sm')] : []));
    return wrap('city', isow(W, HH, E.map(epx).join('') + CY.faces.map(fpx).join('') + L.map(lpx).join(''))); };

  /* ── cartridges on a shelf, as deep as they are large ── */
  R.shelf = (d, H, o) => { const rw = rows(d).slice(0, 7); if (!rw.length) return EMPTY('a shelf needs items'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const sz = (r) => num(r.size ?? r.value ?? r.gb ?? r.params ?? 1); const hi = Math.max(...rw.map(sz)) || 1; const pal = palOf(o);
    const CT = isoBuild([{ u: 0, v: 0, z: 0, w: rw.length * 1.35 + .45, d: 1.8, h: .15, col: DK }].concat(rw.flatMap((c, i) => { const col = c.col || pal(i); return [{ u: .3 + i * 1.35, v: .2, z: .15, w: 1.1, d: .35 + sz(c) / hi * 1.2, h: .55, col }, { u: .45 + i * 1.35, v: .28, z: .7, w: .8, d: .22, h: .04, col: 'color-mix(in srgb,' + col + ' 40%,#fff)' }]; })), 64, W, HH, (b) => (b.u + b.v) * 100 + b.z);
    const L = rw.map((c, i) => lbl(CT.at, .85 + i * 1.35, 2.1, 0, nameOf(c).split(':')[0], B.t3, 'dn'));
    return wrap('shelf', isow(W, HH, CT.faces.map(fpx).join('') + L.map(lpx).join(''))); };

  /* ── a hand of session cards on the table, the live one lifted, lit and lamped ── */
  R.stack = (d, H, o) => { const kv = keyed(d); if (kv.length && !rows(d).length) return R['stacked-bar'](d, H, o);   // the chat-era stack: parts as a stacked bar
    const rw = rows(d).slice(0, 9); if (!rw.length) return EMPTY('a card stack needs items'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const cols = (o && o.draw && o.draw.columns) || 3; const SGX = 2.95, SGY = 2.15, SCW = 2.6, SCD = 1.7; const pal = palOf(o); const pos = (i) => [(i % cols) * SGX, Math.floor(i / cols) * SGY];
    const live = (s) => !!(s.running || s.live || s.current || /running|live|now/.test(String(s.status ?? s.state ?? '')));
    const CB = isoBuild(rw.flatMap((s, i) => { const p = pos(i), lift = live(s) ? .3 : 0; return [{ u: p[0], v: p[1], z: lift, w: SCW, d: SCD, h: .14, col: live(s) ? DK : 'color-mix(in srgb,var(--b-surf3) 72%,var(--b-s3))', cls: live(s) ? 'shine' : '', n: i }, { u: p[0], v: p[1], z: lift + .141, w: SCW, d: .16, h: .001, col: s.col || pal(i) }, { u: p[0] + SCW - .38, v: p[1] + SCD - .38, z: lift + .141, w: .24, d: .24, h: .07, col: live(s) ? B.ac2 : B.s3, cls: live(s) ? 'lamp' : '' }]; }), 62, W, HH, (b) => b.v * 100 + b.u);
    const L = rw.flatMap((s, i) => { const p = pos(i), z = (live(s) ? .3 : 0) + .15; return [lbl(CB.at, p[0] + .2, p[1] + .62, z, nameOf(s), live(s) ? B.t1 : B.t2, 'dn sm' + (live(s) ? ' hd' : ''))].concat(live(s) ? [lbl(CB.at, p[0] + .2, p[1] + 1.2, z, String(s.status ?? s.when ?? 'running'), B.ac2, 'dn sm')] : []); });
    return wrap('stack', isow(W, HH, CB.faces.map(fpx).join('') + L.map(lpx).join(''))); };

  /* ── the radar sweep: a disc on the floor, a blip per event, nearer the middle the more recent ── */
  R.sweep = (d, H, o) => { const ev = evsOf(d).slice(-12); if (!ev.length) return EMPTY('a radar needs events'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H);
    const RD = isoBuild([{ u: -.25, v: -.25, z: 0, w: .5, d: .5, h: .3, col: B.ac2 }], 68, W, HH, null, null, null, { extra: [[-2.8, -2.8, 0], [2.8, 2.8, 0], [2.8, -2.8, 0], [-2.8, 2.8, 0]], padb: 10, padt: 4 });
    const rC = RD.at(0, 0, 0), rR = 2.7 * RD.k; const n = ev.length;
    const pings = ev.map((e, i) => { const age = e.age != null ? num(e.age) : (n - 1 - i) / Math.max(1, n - 1); const r = .3 + age * 2.2, a = ((i * 137) % 360) * Math.PI / 180; const p = RD.at(Math.cos(a) * r, Math.sin(a) * r, 0); return '<span class="ping" style="left:' + p[0].toFixed(1) + 'px;top:' + p[1].toFixed(1) + 'px;background:' + (e.col || (/err|fail/i.test(String(e.kind ?? e.level ?? '')) ? B.ac4 : DV(String(e.kind ?? '').length + i))) + ';--n:' + i + '" title="' + esc(txt(e)) + '"></span>'; });
    return wrap('sweep', isow(W, HH, '<div class="radar" style="left:' + (rC[0] - rR).toFixed(1) + 'px;top:' + (rC[1] - rR).toFixed(1) + 'px;width:' + (rR * 2).toFixed(1) + 'px;height:' + (rR * 2).toFixed(1) + 'px;transform:' + ISO().discTf(RD.P) + '"><div class="disc"></div><div class="ring2"></div><div class="beam"></div></div>' + RD.faces.map(fpx).join('') + pings.join(''))); };

  /* ── the meter panel: one round meter per class, studs lit to the rate, a lamp when hot, a needle over the lot ── */
  R['meter-panel'] = (d, H, o) => { const kv = keyed(d).slice(0, 8); if (!kv.length) return EMPTY('a meter panel needs rates'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const MCOL = (o && o.draw && o.draw.columns) || 4, MP = 1.45, max = num(o && o.draw && o.draw.max) || Math.max(...kv.map((x) => x[1])) * 1.25 || 1, hot = num(o && o.draw && o.draw.hot) || .75; const pos = (i) => [(i % MCOL) * MP, Math.floor(i / MCOL) * MP];
    const VU = isoBuild(kv.flatMap((c, i) => { const p = pos(i), val = Math.min(1, c[1] / max); const out = [{ u: p[0], v: p[1], z: 0, w: 1.2, d: 1.2, h: .16, col: DK }, { u: p[0] + .1, v: p[1] + .1, z: .16, w: 1.0, d: 1.0, h: .03, col: 'color-mix(in srgb,var(--b-ac) 10%,var(--b-s2))' }, { u: p[0] + .52, v: p[1] + .1, z: .19, w: .16, d: .16, h: .05, col: val > hot ? B.ac4 : B.s3, cls: val > hot ? 'lamp' : '' }];
      const N = 11, a0 = Math.PI * .78, sp = Math.PI * 1.44; for (let j = 0; j < N; j++) { const a = a0 + (j / (N - 1)) * sp, on = j / (N - 1) <= val; out.push({ u: p[0] + .6 + Math.cos(a) * .42 - .05, v: p[1] + .6 + Math.sin(a) * .42 - .05, z: .19, w: .1, d: .1, h: on ? .09 : .04, n: i, col: !on ? B.s3 : (j < 7 ? B.ac2 : j < 9 ? B.ac3 : B.ac4) }); } return out; }), 76, W, HH, (b) => (b.u + b.v) * 100 + b.z);
    const needles = kv.flatMap((c, i) => { const p = pos(i), val = Math.min(1, c[1] / max), a = Math.PI * .78 + val * Math.PI * 1.44; return segsAt(VU.at, [[p[0] + .6, p[1] + .6, .28], [p[0] + .6 + Math.cos(a) * .34, p[1] + .6 + Math.sin(a) * .34, .26]], B.t1, 'needle'); });
    const L = kv.flatMap((c, i) => { const p = pos(i); return [lbl(VU.at, p[0] + .6, p[1] + 1.34, 0, c[0], B.t3, 'dn sm'), lbl(VU.at, p[0] + .6, p[1] + .78, .34, fmt(c[1]), B.t1, 'dn')]; });
    return wrap('meter-panel', isow(W, HH, VU.faces.map(fpx).join('') + needles.map(epx).join('') + L.map(lpx).join(''))); };

  /* ── pipes: an iso graph of links, dashes moving at the rate on each; no floor, the links are the point ── */
  R.pipes = (d, H, o) => { if (o && o.draw && o.draw.engine === 'mermaid') return R.diagram(d, H, o); const g = graphOf(d); if (!g.nodes.length) return EMPTY('pipes need nodes and links'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H);
    const cols = Math.ceil(Math.sqrt(g.nodes.length)); const N = g.nodes.map((n, i) => ({ id: n.id, u: n.u != null ? num(n.u) : (i % cols) * 3 + .4 + (Math.floor(i / cols) % 2) * .6, v: n.v != null ? num(n.v) : Math.floor(i / cols) * 3 + .4, col: n.col || n.color || DV(i + 2), rate: n.rate ?? n.label ?? n.value ?? '' }));
    const PIP = isoBuild(N.map((n) => ({ u: n.u - .45, v: n.v - .45, z: 0, w: .9, d: .9, h: .62, col: n.col })), 80, W, HH, (b) => (b.u + b.v) * 100 + b.z); const byId = {}; N.forEach((n) => { byId[n.id] = n; });
    const E = g.links.flatMap((e) => { const a = byId[e.a], b = byId[e.b]; if (!a || !b) return []; const on = e.v > 0 && !(e.label === 'idle'); return segsAt(PIP.at, [[a.u, a.v, .34], [b.u, a.v, .34], [b.u, b.v, .34]], on ? mix(B.ac, 45, B.s3) : B.s3, on ? 'flowe' : ''); });
    const L = N.flatMap((n) => [lbl(PIP.at, n.u, n.v + .62, 0, n.id, B.t2, 'dn hd')].concat(n.rate !== '' ? [lbl(PIP.at, n.u, n.v - .3, .95, String(n.rate), String(n.rate) === '—' ? B.t3 : B.ac2, 'sm')] : []));
    return wrap('pipes', isow(W, HH, E.map(epx).join('') + PIP.faces.map(fpx).join('') + L.map(lpx).join(''))); };
  R['topology@iso'] = (d, H, o) => { const g = graphOf(d); if (!g.nodes.length) return EMPTY('a topology needs nodes'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const I = ISO();
    const floorsN = Math.max(1, Math.min(4, num(o && o.draw && o.draw.floors) || 3)); const depth = layers(g); const lvl = {}; g.nodes.forEach((n) => { lvl[n.id] = n.floor != null ? num(n.floor) : Math.min(floorsN - 1, Math.round((100 - depth[n.id][1]) / 100 * (floorsN - 1))); });
    const perFloor = {}; g.nodes.forEach((n) => { (perFloor[lvl[n.id]] = perFloor[lvl[n.id]] || []).push(n); }); const pos = {}; Object.keys(perFloor).forEach((f) => { perFloor[f].forEach((n, i, arr) => { pos[n.id] = [n.u != null ? num(n.u) : 1 + (i + .5) / arr.length * 4, n.v != null ? num(n.v) : .8 + (i % 2) * 1.4, +f * 1.5]; }); });
    const PT = I.proj(28, 42, 34); const floorCols = [B.s3, 'color-mix(in srgb,var(--b-ac) 30%,var(--b-s3))', 'color-mix(in srgb,var(--b-ac5) 30%,var(--b-s3))', 'color-mix(in srgb,var(--b-ac2) 30%,var(--b-s3))'];
    const floors = Array.from({ length: floorsN }, (_, i) => ({ u: 0, v: 0, z: i * 1.5, w: 6, d: 3, h: .06, col: floorCols[i] })); const pal = palOf(o);
    const tf = I.scene(PT, floors.concat(g.nodes.map((n, i) => { const p = pos[n.id]; return { u: p[0] - .3, v: p[1] - .3, z: p[2] + .06, w: .6, d: .6, h: .5, col: n.col || n.color || pal(i) }; })), (b) => b.z * 1000 + b.u + b.v); const sh = I.fit(tf, W, HH - 10); const at = (u, v, z) => { const p = PT(u, v, z); return [p[0] + sh.dx, p[1] + sh.dy]; };
    const E = g.links.flatMap((e, i) => { const a = pos[e.a], b = pos[e.b]; if (!a || !b) return []; const w = I.route({ u: a[0], v: a[1], z: a[2] + .56 }, { u: b[0], v: b[1], z: b[2] + .56 }, ((i % 3) - 1) * .12, 3); return I.segs(w.map((q) => at(q[0], q[1], q[2]))).map((s) => ({ x: s.x.toFixed(1) + 'px', y: s.y.toFixed(1) + 'px', len: s.len.toFixed(1) + 'px', deg: s.deg.toFixed(1) + 'deg', col: a[2] !== b[2] ? B.ac : DV(i), cls: '' })); });
    const L = Object.keys(perFloor).map((f) => { const n = perFloor[f][0]; const p = pos[n.id]; return lbl(at, p[0], p[1], p[2] + .95, String((d && d.floors && d.floors[f]) || n.floorName || n.label || n.id), B.t2); });
    return wrap('topology', isow(W, HH, I.px(tf).map(fpx).join('') + E.map(epx).join('') + L.map(lpx).join(''))); };

  /* ── the workplace set, isometric: terminal (a frame), table, files, feed, board, agenda, log ── */
  R['table@iso'] = (d, H, o) => { const rw = rows(d).slice(0, 6); if (!rw.length) return EMPTY('no rows'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const cols = Object.keys(rw[0]).filter((k) => typeof rw[0][k] !== 'object').slice(0, 4); const lit = o && o.draw && o.draw.lit != null ? num(o.draw.lit) : null; const litCol = cols.find((c) => typeof rw[0][c] === 'number' && (o && o.draw && o.draw.sort === c)) || cols.find((c) => /temp/.test(c)) || cols.find((c) => typeof rw[0][c] === 'number');
    const hot = (r) => lit != null && litCol && num(r[litCol]) >= lit;
    const ITB = isoBuild([{ u: 0, v: 0, z: 0, w: 5, d: .5 + rw.length * .56 + .1, h: .16, col: DK }, { u: 0, v: 0, z: .16, w: 5, d: .42, h: .05, col: B.ac }].concat(rw.map((r, i) => ({ u: 0, v: .5 + i * .56, z: .16, w: 5, d: .5, h: hot(r) ? .09 : .03, col: hot(r) ? B.ac4 : (i % 2 ? 'var(--b-surf2)' : B.s3), cls: hot(r) ? 'hot' : '', n: i }))).concat(cols.slice(1).map((c, i) => ({ u: 1.8 + i * 1.1, v: 0, z: .16, w: .04, d: .5 + rw.length * .56 + .1, h: .06, col: B.bd2 }))), 40, W, HH, (b) => b.v * 100 + b.z, null, null, { padt: 4 });
    const L = cols.map((c, i) => lbl(ITB.at, i ? 2.4 + (i - 1) * 1.1 : .9, .21, .3, c, B.t1, 'sm')).concat(rw.map((r, i) => lbl(ITB.at, .9, .75 + i * .56, .3, cols.map((c) => typeof r[c] === 'number' ? fmt(r[c]) : String(r[c] ?? '')).join('  '), hot(r) ? B.ac4 : B.t2, 'sm')));
    return wrap('table', isow(W, HH, ITB.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };
  R['files@iso'] = (d, H, o) => { const rw = rows(d).slice(0, 6); if (!rw.length) return EMPTY('no rows'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const pal = palOf(o); const n = rw.length;
    const IFB = isoBuild([{ u: -.2, v: -.2, z: 0, w: 3.8, d: 2.9, h: .1, col: DK }].concat(rw.slice().reverse().flatMap((f, i) => { const k = n - 1 - i, top = k === 0, col = f.col || pal(k); return [{ u: .2 + k * .12, v: .2 + k * .16, z: .1 + i * .09 + (top ? .35 : 0), w: 2.6, d: 2.0, h: .07, col: 'color-mix(in srgb,' + col + ' 30%,var(--b-surf2))', cls: top ? 'shine' : '', n: i }, { u: .2 + k * .12, v: .2 + k * .16, z: .1 + i * .09 + (top ? .35 : 0) + .07, w: .16, d: 2.0, h: .02, col }]; })), 42, W, HH, (b) => b.z * 100 + b.v);
    const L = rw.map((f, k) => lbl(IFB.at, 3.0 + k * .12, .55 + k * .16, .1 + (n - 1 - k) * .09 + (k === 0 ? .42 : .07), String(f.path ?? f.name ?? '').split('/').pop(), k === 0 ? B.t1 : B.t3, 'sm')).concat([lbl(IFB.at, 1.7, 3.1, 0, String((o && o.title) || '') + ' · ' + n + ' files · newest on top', B.t3, 'dn sm')]);
    return wrap('files', isow(W, HH, IFB.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };
  R['feed@iso'] = (d, H, o) => { const ev = evsOf(d).slice(0, 5); if (!ev.length) return EMPTY('a feed needs stories'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const cur = Math.max(0, Math.min(ev.length - 1, +ui(o, 'page', Math.min(1, ev.length - 1)) || 0));
    const IFE = isoBuild([{ u: 0, v: 1.0, z: 0, w: ev.length + .4, d: .5, h: .1, col: DK }].concat(ev.map((s, i) => ({ u: .4 + i * 1.0, v: 1.05 + (i === cur ? -.35 : 0), z: .1, w: .9, d: .06, h: 1.5 + (i === cur ? .2 : 0), col: i === cur ? DK : 'color-mix(in srgb,var(--b-surf2) 80%,var(--b-s3))', cls: i === cur ? 'shine' : '', n: i }))).concat(ev.map((s, i) => ({ u: .48 + i * 1.0, v: 1.04 + (i === cur ? -.35 : 0), z: 1.35 + (i === cur ? .2 : 0), w: .74, d: .005, h: .08, col: i === cur ? B.ac : B.t3 }))), 40, W, HH, (b) => b.v * 100 + b.u, null, null, { padt: 6 });
    const r = ev[cur]; const L = [lbl(IFE.at, 1.85, .4, 1.1, String(r.title ?? txt(r)).slice(0, 40), B.t1, 'sm hd'), lbl(IFE.at, 1.85, .4, .8, [r.who ?? r.author, hhmm(r.when ?? r.t ?? r.ts), String(r.body ?? '').slice(0, 48)].filter(Boolean).join(' · '), B.t3, 'sm'), lbl(IFE.at, 2.6, 1.9, 0, (cur + 1) + ' of ' + ev.length + (o && o.title ? ' · ' + o.title : ''), B.t3, 'dn sm')];
    return wrap('feed', isow(W, HH, IFE.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };
  R['board@iso'] = (d, H, o) => { let cols = []; if (d && typeof d === 'object' && !Array.isArray(d) && Array.isArray(d.columns)) cols = d.columns.map((c) => ({ n: nameOf(c), items: rows(c.items || c.cards || []) })); else { const rw = rows(d); const by = {}, order = []; rw.forEach((r) => { const k = String(r.column ?? r.lane ?? r.status ?? r.state ?? 'to do'); if (!by[k]) { by[k] = []; order.push(k); } by[k].push(r); }); cols = order.map((k) => ({ n: k, items: by[k] })); }
    if (!cols.length) return EMPTY('a board needs columns of cards'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const kc = (n, i) => /done|merged/.test(n) ? B.ac2 : /doing|progress|running/.test(n) ? B.ac : /waiting|blocked/.test(n) ? B.ac3 : DV(i + 2); const doing = cols.findIndex((c) => /doing|progress|running/.test(c.n.toLowerCase()));
    const IKN = isoBuild(cols.slice(0, 4).flatMap((c, i) => [{ u: i * 1.9, v: 0, z: 0, w: 1.6, d: 2.4, h: .14, col: DK }].concat(Array.from({ length: Math.min(9, c.items.length) }, (_, j) => ({ u: i * 1.9 + .18, v: .2 + (j % 3) * .06, z: .14 + j * .12, w: 1.24, d: 1.9, h: .09, col: 'color-mix(in srgb,' + kc(c.n.toLowerCase(), i) + ' ' + (j === c.items.length - 1 ? 55 : 25) + '%,var(--b-surf2))', cls: (i === doing && j === c.items.length - 1) ? 'shine' : '', n: j })))), 40, W, HH, (b) => b.u * 100 + b.z);
    const L = cols.slice(0, 4).flatMap((c, i) => [lbl(IKN.at, i * 1.9 + .8, 2.7, 0, c.n, B.t3, 'dn sm'), lbl(IKN.at, i * 1.9 + .8, .95, .14 + Math.min(9, c.items.length) * .12 + .3, String(c.items.length), B.t1, 'sm hd')]);
    return wrap('board', isow(W, HH, IKN.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };
  R['agenda@iso'] = (d, H, o) => { const ev = evsOf(d); if (!ev.length) return EMPTY('an agenda needs bookings'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H);
    const hrs = (r) => { const m = hhmm(r.when ?? r.t ?? r.start ?? r.time).match(/(\d{1,2}):(\d{2})/); return m ? +m[1] + (+m[2]) / 60 : 9; }; const dur = (r) => { const w = String(r.duration ?? r.w ?? ''); const m = w.match(/(\d+(?:\.\d+)?)\s*(h|m)?/); return m ? (m[2] === 'm' ? +m[1] / 60 : +m[1]) : .5; }; const lo = Math.max(0, Math.floor(Math.min(...ev.map(hrs))) - 1), span = Math.max(8, Math.ceil(Math.max(...ev.map((r) => hrs(r) + dur(r)))) + 1 - lo); const nowH = new Date().getHours() + new Date().getMinutes() / 60; let cur = ev.findIndex((r) => r.now || r.current); if (cur < 0) { ev.forEach((r, i) => { if (hrs(r) <= nowH) cur = i; }); }
    const IAB = isoBuild([{ u: 0, v: 0, z: 0, w: span * .4, d: .9, h: .12, col: DK }].concat(Array.from({ length: Math.floor(span / 2) + 1 }, (_, i) => ({ u: i * .8, v: .05, z: .12, w: .03, d: .2, h: .06, col: B.bd2 }))).concat(ev.slice(0, 8).map((a, i) => ({ u: (hrs(a) - lo) * .4, v: .25, z: .12, w: Math.max(.1, dur(a) * .4), d: .5, h: i === cur ? .34 : .2, col: a.col || a.color || DV(i), cls: i === cur ? 'lamp' : '', n: i }))).concat(nowH >= lo && nowH <= lo + span ? [{ u: (nowH - lo) * .4, v: .1, z: .5, w: .04, d: .7, h: .4, col: B.t1 }] : []), 40, W, HH, (b) => b.v * 100 + b.u, null, null, { padt: 8 });
    const L = Array.from({ length: Math.floor(span / 4) + 1 }, (_, i) => lbl(IAB.at, i * 1.6, 0, .3, String(lo + i * 4).padStart(2, '0'), B.t3, 'sm')).concat(ev.slice(0, 8).map((a, i) => lbl(IAB.at, (hrs(a) - lo) * .4 + dur(a) * .2, .5, (i === cur ? .34 : .2) + .34, String(a.title ?? a.name ?? txt(a)).slice(0, 14), i === cur ? B.t1 : B.t3, 'sm')));
    return wrap('agenda', isow(W, HH, IAB.faces.map(fpx).join('') + L.map(lpx).join('')), 'vb-iso'); };
  R['log@iso'] = (d, H, o) => { const ev = evsOf(d).slice(-6); if (!ev.length) return EMPTY('a log needs rows with text'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const bad = (r) => /err|fail|crit/i.test(String(r.kind ?? r.level ?? ''));
    const ILB = isoBuild([{ u: -.6, v: .2, z: -.05, w: 7.4, d: 1.1, h: .08, col: DK }].concat(ev.map((r, n) => ({ u: .2, v: .35, z: .03, w: .9, d: .8, h: .07, col: bad(r) ? B.ac4 : (n % 2 ? 'var(--b-surf2)' : B.s3), cls: 'mv', n }))), 40, W, HH, (b) => b.cls === 'mv' ? 1e6 + b.n : (b.u + b.v) * 100 + b.z);
    const a = ILB.at(.2, .35, .03), bb = ILB.at(5.6, .35, .03); const alerts = ev.filter(bad).length; const L = [lbl(ILB.at, 3.1, 1.6, 0, String((o && o.title) || 'log') + ' · ' + alerts + ' alert' + (alerts === 1 ? '' : 's'), B.t3, 'dn sm'), lbl(ILB.at, 3.1, .2, .6, ev.map((r) => txt(r).slice(0, 22)).join(' · ').slice(0, 90), B.t2, 'sm')];
    return wrap('log', isow(W, HH, ILB.faces.map(fpx).join('') + L.map(lpx).join(''), '--cx:' + (bb[0] - a[0]).toFixed(1) + 'px;--cy:' + (bb[1] - a[1]).toFixed(1) + 'px'), 'vb-iso'); };

  /* ── the Widgets board's iso forms: columns, tiles, blocks, cubes, terraces, gantt, calendar, scatter, funnel, stacked ── */
  R['column@iso'] = (d, H, o) => { const kv = keyed(d); const vals = (kv.length ? kv.map((x) => x[1]) : series(d)).slice(-16); if (!vals.length) return EMPTY('columns need values'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = Math.max(...vals) || 1, pal = palOf(o, 'load'), cols = Math.max(2, Math.ceil(Math.sqrt(vals.length)));
    const S = sceneOf('column', vals.map((v, i) => ({ u: i % cols, v: Math.floor(i / cols), z: 0, w: .72, d: .72, h: .25 + v / hi * 3.2, col: pal(i, v, hi), t: (kv[i] ? kv[i][0] + ' · ' : '') + fmt(v) })), 27, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['bars@iso'] = (d, H, o) => R['column@iso'](d, H, o);
  R['heat@iso'] = (d, H, o) => { const m = cellsOf(d); if (!m.rows.length) return EMPTY('a heat map needs rows of numbers'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = Math.max(...m.rows.flatMap((r) => r.v.map(num))) || 1; const pal = palOf(o);
    const S = sceneOf('heat', m.rows.slice(0, 6).flatMap((r, ri) => r.v.slice(0, 12).map((x, i) => ({ u: i, v: ri, z: 0, w: .82, d: .82, h: .1 + num(x) / hi * 2.2, col: pal(ri), t: r.n + ' · ' + fmt(x) }))), 19, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['treemap@iso'] = (d, H, o) => { const kv = keyed(d).slice().sort((a, b) => b[1] - a[1]).slice(0, 8); if (!kv.length) return EMPTY('a treemap needs parts'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = kv[0][1] || 1, pal = palOf(o);
    const S = sceneOf('treemap', kv.map((x, i) => ({ u: (i % 4) * 1.1, v: Math.floor(i / 4) * 1.1, z: 0, w: 1, d: 1, h: .2 + x[1] / hi * 2.4, col: pal(i), t: x[0] + ' · ' + fmt(x[1]) })), 28, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['waffle@iso'] = (d, H, o) => { const kv = keyed(d).slice(0, 6); if (!kv.length) return EMPTY('a waffle needs parts'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const tot = kv.reduce((s, x) => s + Math.abs(x[1]), 0) || 1; const pal = palOf(o); const fills = []; kv.forEach((x, i) => { const n = Math.round(Math.abs(x[1]) / tot * 60); for (let j = 0; j < n && fills.length < 60; j++) fills.push(i === 0 ? B.ac : pal(i)); }); while (fills.length < 60) fills.push(null);
    const S = sceneOf('waffle', fills.map((c, i) => ({ u: i % 10, v: Math.floor(i / 10), z: 0, w: .8, d: .8, h: c ? .8 : .12, col: c || B.s3 })), 14, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['small-multiples@iso'] = (d, H, o) => { const ms = multi(d).slice(0, 6); if (!ms.length) return EMPTY('terraces need series'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = Math.max(...ms.flatMap((s) => s.v)) || 1;
    const S = sceneOf('small-multiples', ms.flatMap((s, ri) => s.v.slice(-12).map((v, i) => ({ u: i, v: ri * 1.15, z: 0, w: .9, d: .9, h: .1 + v / hi * 1.6, col: s.col || (s.r && /timeout|down/.test(String(s.r.status ?? '')) ? B.ac3 : B.ac), t: s.n + ' · ' + fmt(v) }))), 15, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['gantt@iso'] = (d, H, o) => { const st = stagesOf(d).slice(0, 8); if (!st.length) return EMPTY('a gantt needs rows'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const s0 = st.map((s) => num(s.start ?? s.x ?? 0)), s1 = st.map((s, i) => num(s.end ?? (s0[i] + num(s.duration ?? s.w ?? 1)))); const lo = Math.min(...s0), hi = Math.max(...s1), sp = (hi - lo) || 1;
    const S = sceneOf('gantt', st.map((s, i) => ({ u: (s0[i] - lo) / sp * 10, v: i * 1.05, z: 0, w: Math.max(.2, (s1[i] - s0[i]) / sp * 10), d: .8, h: s.st === 'now' ? .9 : .45, col: s.st === 'done' ? B.ac2 : s.st === 'now' ? B.ac : s.st === 'bad' ? B.ac4 : B.t3, t: nameOf(s) })), 22, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['calendar@iso'] = (d, H, o) => { let by = {}; const rw = rows(d); if (d && typeof d === 'object' && !Array.isArray(d) && d.days && typeof d.days === 'object') Object.keys(d.days).forEach((k) => { by[dayOf(k)] = num(d.days[k]); }); else rw.forEach((r) => { const k = dayOf(r.when ?? r.date ?? r.day ?? r.t); if (k) by[k] = (by[k] || 0) + (r.value != null ? num(r.value) : 1); });
    const days = Object.keys(by).sort().slice(-84); if (!days.length) return EMPTY('a calendar needs dated rows'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = Math.max(...days.map((k) => by[k])) || 1;
    const S = sceneOf('calendar', days.map((k, i) => ({ u: Math.floor(i / 7), v: i % 7, z: 0, w: .78, d: .78, h: .08 + by[k] / hi * 1.6, col: 'color-mix(in srgb,var(--b-ac2) ' + Math.round(20 + by[k] / hi * 80) + '%,var(--b-s3))', t: k + ' · ' + fmt(by[k]) })), 16, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['scatter@iso'] = (d, H, o) => { const pts = rows(d).map((p) => ({ x: num(p.x ?? p.t ?? p[0]), y: num(p.y ?? p.v ?? p[1]), s: num(p.size ?? p.r ?? p.calls ?? 3), k: String(p.kind ?? p.class ?? '') })).slice(0, 40); if (pts.length < 2) return EMPTY('a scatter needs points'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y); const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys), s1 = Math.max(...pts.map((p) => p.s)) || 1; const kinds = []; const pal = palOf(o);
    const S = sceneOf('scatter', pts.map((p) => { let ki = kinds.indexOf(p.k); if (ki < 0) { kinds.push(p.k); ki = kinds.length - 1; } return { u: (p.x - x0) / ((x1 - x0) || 1) * 7, v: (p.y - y0) / ((y1 - y0) || 1) * 7, z: 0, w: .5, d: .5, h: .3 + p.s / s1 * 2.5, col: pal(ki), t: p.k + ' ' + fmt(p.x) + ' · ' + fmt(p.y) }; }), 19, W, HH, byStack); return typeof S === 'string' ? S : S.html; };
  R['funnel@iso'] = (d, H, o) => { const st = stagesOf(d); const kv = st.length && st.some((s) => s.value != null || s.count != null) ? st.map((s) => [nameOf(s), valOf(s)]) : keyed(d); if (!kv.length) return EMPTY('a funnel needs stages with counts'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const hi = kv[0][1] || 1, pal = palOf(o);
    const S = sceneOf('funnel', kv.slice(0, 7).map((f, i) => { const w = Math.max(2.5, f[1] / hi * 10); return { u: (10 - w) / 2, v: 0, z: i * .42, w, d: 1.6, h: .4, col: i === kv.length - 1 ? B.ac2 : i === 0 ? B.ac : pal(i), t: f[0] + ' · ' + fmt(f[1]) }; }), 22, W, HH, (b) => b.z); return typeof S === 'string' ? S : S.html; };
  R['area@iso'] = (d, H, o) => { const ms = multi(d); if (!ms.length) return EMPTY('a stacked wall needs series'); if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const N = Math.min(12, Math.max(...ms.map((s) => s.v.length))); const tot = Array.from({ length: N }, (_, i) => ms.reduce((s, m) => s + (m.v[m.v.length - N + i] || 0), 0)); const hi = Math.max(...tot) || 1; const pal = palOf(o);
    const S = sceneOf('area', Array.from({ length: N }, (_, i) => { let z = 0; return ms.map((s, si) => { const h = (s.v[s.v.length - N + i] || 0) / hi * 3.3; const b = { u: i, v: 0, z, w: .8, d: 1.2, h, col: s.col || pal(si), t: s.n }; z += h; return b; }); }).flat(), 18, W, HH, (b) => b.u * 1000 + b.z); return typeof S === 'string' ? S : S.html; };
  // the racks (the Fleet carousel's third page): three racks, one slot per row, colour by state
  R.racks = (rw, H, o) => { if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const list = rows(rw).slice(0, 30); const per = 10;
    const S = sceneOf('racks', list.map((r, i) => ({ u: Math.floor(i / per) * 1.7, v: 0, z: (i % per) * .34, w: 1, d: .9, h: .28, col: stCol(r.status ?? r.state), t: nameOf(r) + ' · ' + String(r.status ?? r.state ?? ''), row: r, g: 'rack' + Math.floor(i / per) })), 21, W, HH, (b) => b.u * 1000 + b.z); return typeof S === 'string' ? S : '<div class="vb-carp">' + S.html + '</div>'; };

  /* ── the WidgetsIso board: a site's parts and Vera's suites as objects on a plate ── */
  const plate = (w, dd, col) => ({ u: -.3, v: -.3, z: -.12, w: w + .6, d: dd + .6, h: .12, col: col || B.s3 });
  const Bx = (u, v, z, w, dd, h, col, cls, t, row) => Object.assign({ u, v, z, w, d: dd, h, col, cls: cls || '', t: t || '' }, row && typeof row === 'object' ? { row } : {});   // a block drawn for a row carries it: its hover detail and its deep dive
  const rng = (i) => Math.abs(Math.sin(i * 12.9898) * 43758.5453) % 1;
  const isoForm = (form, boxes, o, H, opts) => { if (!ISO()) return ISO_WAIT; const W = wof(o), HH = hof(H); const S = sceneOf(form, boxes, 1, W, HH, (opts && opts.key) || byStack, Object.assign({ tilt: 32, azim: 42, padx: 8, padt: 6, padb: 6, max: 99 }, opts || {})); return typeof S === 'string' ? S : S.html; };
  R.library = (d, H, o) => { const rw = rows(d).slice(0, 12); if (!rw.length) return EMPTY('a library needs documents'); const docs = rw.filter((r) => !r.folder && !/folder|dir/.test(String(r.kind ?? ''))), folders = rw.filter((r) => r.folder || /folder|dir/.test(String(r.kind ?? '')));
    return isoForm('library', [plate(6.4, 1.6)].concat(docs.slice(0, 9).map((r, i) => Bx(i * .62, .4, 0, .34, .08, .6 + (num(r.pages ?? r.size ?? 0) ? Math.min(1, num(r.pages ?? r.size) / 50) * .7 : rng(i) * .7), i % 3 === 0 ? DV(0) : B.t2, '', nameOf(r), r))).concat(folders.length ? [Bx(5.8, .2, 0, .7, .9, .5, DV(2), '', folders.length + ' folders'), Bx(5.85, .25, .5, .3, .2, .1, DV(2))] : []), o, H); };
  R.pages = (d, H, o) => { const rw = rows(d).slice(0, 9); if (!rw.length) return EMPTY('site pages need items'); const home = Math.max(0, rw.findIndex((r) => r.home || r.current || /home|index/.test(String(r.name ?? r.path ?? ''))));
    return isoForm('pages', [plate(4.6, 3)].concat(rw.map((r, i) => Bx((i % 3) * 1.55, Math.floor(i / 3) * 1.55, 0, 1.3, 1.2, i === home ? .32 : .08, i === home ? B.ac : B.t3, '', nameOf(r), r))), o, H); };
  R['list@iso'] = (d, H, o) => { const rw = rows(d).slice(0, 8); const str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const items = rw.length ? rw : str.map((s) => ({ name: s })); if (!items.length) return EMPTY('no rows');
    return isoForm('list', [plate(4.6, items.length * .52 + .2)].concat(items.flatMap((r, i) => [Bx(0, i * .52, 0, 3.6, .38, .1, i % 2 ? B.t3 : B.bd2, '', nameOf(r), r), Bx(3.85, i * .52 + .04, 0, .3, .3, .3, stCol(r.status ?? r.state) === B.t3 ? B.ac2 : stCol(r.status ?? r.state))])), o, H); };
  R['people@iso'] = (d, H, o) => { const rw = rows(d).slice(0, 12); if (!rw.length) return EMPTY('people need rows'); const lv = (r) => num(r.level ?? (r.reports_to || r.manager ? 1 : 0)); const L0 = rw.filter((r) => lv(r) === 0), L1 = rw.filter((r) => lv(r) === 1), L2 = rw.filter((r) => lv(r) >= 2);
    return isoForm('people', [plate(6.2, 3.6)].concat(L0.slice(0, 3).map((r, i) => Bx(2.7 - (L0.length - 1) * .5 + i, 0, 0, .6, .6, 1.3, B.ac, '', nameOf(r), r))).concat(L1.slice(0, 5).map((r, i) => Bx(.7 + i * (5 / Math.max(1, L1.length - 1) || 2), 1.3, 0, .55, .55, .95, DV(5), '', nameOf(r), r))).concat(L2.slice(0, 6).map((r, i) => Bx(.2 + i * 1.05, 2.7, 0, .45, .45, .6, B.t2, '', nameOf(r), r))).concat([Bx(.9, .6, 0, 4.4, .08, .05, B.bd2), Bx(.4, 1.95, 0, 5.4, .08, .05, B.bd2)]), o, H, { tilt: 30 }); };
  R.notices = (d, H, o) => { const ev = evsOf(d).slice(0, 5); if (!ev.length) return EMPTY('notices need posts'); const P = [[.3, 1.6], [1.4, 1.9], [2.5, 1.5], [.5, .4], [2.2, .5]]; const newest = ev.findIndex((r) => r.new || r.unread) >= 0 ? ev.findIndex((r) => r.new || r.unread) : 0;
    return isoForm('notices', [plate(4.4, .6), Bx(0, .2, 0, 4, .12, 2.6, B.bd2)].concat(ev.map((r, i) => Bx(P[i][0], .32, P[i][1], .8, .05, .7, i === newest ? B.ac3 : [B.t2, DV(0), DV(1), B.t2, DV(6)][i], '', txt(r), r))), o, H, { tilt: 26 }); };
  R.approvals = (d, H, o) => { const st = stagesOf(d).slice(0, 5); if (!st.length) return EMPTY('approvals need stages'); const waiting = st.findIndex((s) => s.st === 'now' || /wait/.test(String(s.status ?? '')));
    return isoForm('approvals', [plate(st.length * 1.6, 1.6)].concat(st.flatMap((s, i) => { const col = s.st === 'done' ? B.ac2 : i === waiting ? B.ac3 : B.bd2; return [Bx(i * 1.6, .4, 0, .8, .8, .3, col, '', nameOf(s), s), Bx(i * 1.6 + .2, .6, .3, .4, .4, .35, col, i === waiting ? 'lamp' : ''), i < st.length - 1 ? Bx(i * 1.6 + .85, .75, 0, .7, .1, .05, B.t3) : null].filter(Boolean); })), o, H); };
  R.wiki = (d, H, o) => { const rw = rows(d); const n = rw.length || num(d && d.pages) || 6; const open = (d && d.open) || (rw[0] && nameOf(rw[0])) || '';
    return isoForm('wiki', [plate(5.4, 3.2), Bx(0, .3, 0, 1.9, 2.4, .14, B.t2, '', open), Bx(2.1, .3, 0, 1.9, 2.4, .14, B.t2), Bx(1.9, .3, 0, .2, 2.4, .26, DV(4)), Bx(.3, .7, .14, 1.3, .1, .02, B.bd2), Bx(.3, 1.1, .14, 1.3, .1, .02, B.bd2), Bx(2.4, .7, .14, 1.3, .1, .02, B.bd2)].concat(Array.from({ length: Math.min(8, n) }, (_, i) => Bx(4.5, .4 + i * .4, 0, .5, .3, .12, B.t3))), o, H, { tilt: 34 }); };
  R['form@iso'] = (d, H, o) => { const kv = keyed(d); const rw = rows(d); const fields = rw.length ? rw.map((r) => ({ n: nameOf(r), req: !!(r.required || r.lit) })) : (kv.length ? kv.map((x) => ({ n: x[0] })) : Object.keys((d && typeof d === 'object' && !Array.isArray(d)) ? d : {}).map((k) => ({ n: k, req: false }))); if (!fields.length) return EMPTY('a form needs fields'); const req = Math.max(0, fields.findIndex((f) => f.req));
    return isoForm('form', [plate(3.6, fields.length * .75 + 1.1), Bx(0, 0, 0, 3, fields.length * .75 + .6, .1, B.bd2)].concat(fields.slice(0, 6).map((f, i) => Bx(.3, .3 + i * .75, .1, 2.4, .42, .08, i === req ? B.ac : B.t3, '', f.n))).concat([Bx(2.1, fields.length * .75 + .15, .1, .6, .35, .3, B.ac2, '', 'submit')]), o, H); };
  R.devices = (d, H, o) => { const rw = rows(d).slice(0, 8); if (!rw.length) return EMPTY('devices need rows'); const gw = rw.findIndex((r) => r.gateway || /gateway|hub/.test(String(r.kind ?? r.role ?? ''))); const nodes = rw.filter((_, i) => i !== gw);
    return isoForm('devices', [plate(6.4, 1.8)].concat(nodes.slice(0, 6).flatMap((r, i) => [Bx(.2 + i * .95, .6, 0, .7, .5, .28, /stale|down|off/.test(String(r.status ?? r.state ?? '')) ? B.ac3 : DV(1), '', nameOf(r), r), Bx(.5 + i * .95, .8, .28, .08, .08, .7, B.t2)])).concat(gw >= 0 ? [Bx(5.9, .3, 0, .4, 1.1, .8, DV(1), '', nameOf(rw[gw])), Bx(6.05, .8, .8, .08, .08, 1, B.t1)] : []), o, H); };
  R.notebook = (d, H, o) => { const rw = rows(d).slice(0, 9); if (!rw.length) return EMPTY('a notebook needs cells'); const isCode = (r) => /code|exec|python/.test(String(r.kind ?? r.type ?? r.cell_type ?? '')) || r.source != null; const running = rw.findIndex((r) => r.running || /running/.test(String(r.status ?? '')));
    return isoForm('notebook', [plate(4.2, rw.length * .5 + .2)].concat(rw.map((r, i) => Bx(0, i * .5, 0, 3.6, .38, isCode(r) ? .3 : .08, i === running ? B.ac : isCode(r) ? B.ac2 : B.t3, '', String(r.title ?? r.source ?? r.text ?? '').slice(0, 40), r))).concat(running >= 0 ? [Bx(3.75, running * .5, 0, .3, .3, .5, B.ac3)] : []), o, H); };
  R['gallery@iso'] = (d, H, o) => { const rw = rows(d).slice(0, 8); if (!rw.length) return EMPTY('a gallery needs items');
    return isoForm('gallery', [plate(5.2, 2.6)].concat(rw.flatMap((g, i) => { const u = (i % 4) * 1.25, v = Math.floor(i / 4) * 1.3; return [Bx(u, v + .4, 0, 1, .08, .9, g.pinned ? B.ac : B.bd2, '', nameOf(g), g), Bx(u + .1, v + .36, .1, .8, .06, .7, DV(i))]; })), o, H, { tilt: 28 }); };
  R['pipeline@iso'] = (d, H, o) => { const st = stagesOf(d).slice(0, 5); if (!st.length) return EMPTY('a pipeline needs stages'); const runs = rows(d && d.runs || []).slice(0, 6); const SC = [DV(0), DV(5), B.ac3, B.ac2, DV(2)];
    return isoForm('pipeline', st.map((s, i) => Bx(i * 1.6, .4, 0, 1.4, 1.6, .1, s.st === 'done' ? B.ac2 : s.st === 'now' ? B.ac3 : SC[i % 5], '', nameOf(s), s)).concat(runs.map((r, j) => { const si = Math.max(0, st.findIndex((s) => nameOf(s) === String(r.stage ?? r.status ?? ''))); return Bx(si * 1.6 + .3 + (j % 2) * .6, .7 + Math.floor(j / 2) * .6, .1, .4, .4, r.running || /running/.test(String(r.status ?? '')) ? .4 : .3, r.running ? B.ac : B.t2, '', nameOf(r), r); })).concat([Bx(0, -.6, .6, st.length * 1.6, .5, .08, DV(4)), Bx(st.length * .7, -.5, .68, .3, .3, .3, DV(4))]), o, H); };
  R.datasets = (d, H, o) => { const rw = rows(d).slice(0, 12); if (!rw.length) return EMPTY('datasets need rows'); const tier = (r) => Math.min(2, num(r.tier ?? r.shelf ?? 0)); const by = [0, 1, 2].map((t) => rw.filter((r) => tier(r) === t)); const hi = Math.max(...rw.map((r) => num(r.records ?? r.count ?? r.size ?? 1))) || 1;
    return isoForm('datasets', [0, 1, 2].map((s) => Bx(0, 0, s * 1.1, 5.4, 1.2, .1, B.bd2)).concat(by.flatMap((list, s) => list.slice(0, 5).map((r, i) => Bx(.2 + i * 1.05, .3, s * 1.1 + .1, .7, .7, .55 + num(r.records ?? r.count ?? r.size ?? 1) / hi * .3, r.col || DV((s * 5 + i)), '', nameOf(r), r)))), o, H, { tilt: 30 }); };
  R.hosts = (d, H, o) => { const rw = rows(d).slice(0, 4); if (!rw.length) return EMPTY('hosts need rows');
    return isoForm('hosts', [plate(rw.length * 2.1, 2.6)].concat(rw.flatMap((h, i) => [Bx(i * 2.1, 0, 0, 1.1, 1.1, 1.8, DV(3), '', nameOf(h), h)].concat((Array.isArray(h.guests) ? h.guests : Array.from({ length: num(h.guests ?? h.vms ?? 4) }, (_, g) => ({ name: 'guest ' + (g + 1) }))).slice(0, 6).map((g, gi) => Bx(i * 2.1 + (gi % 2) * .55, 1.4 + Math.floor(gi / 2) * .55, 0, .45, .45, .35, /down|stopped/.test(String((g && g.status) || '')) ? B.ac4 : DV(2), '', typeof g === 'object' ? nameOf(g) : String(g), typeof g === 'object' ? g : null))))), o, H); };
  R.containers = (d, H, o) => { const rw = rows(d).slice(0, 18); if (!rw.length) return EMPTY('containers need rows'); const hosts = []; rw.forEach((r) => { const h = String(r.host ?? r.node ?? 'host'); if (!hosts.includes(h)) hosts.push(h); }); const per = {};
    return isoForm('containers', [plate(hosts.length * 2.8, 2.6)].concat(rw.map((c, i) => { const h = hosts.indexOf(String(c.host ?? c.node ?? 'host')); const k = (per[h] = (per[h] || 0) + 1) - 1; const col = k % 3, row = Math.floor(k / 3); const stopped = /stopped|exited|dead/.test(String(c.status ?? c.state ?? '')); return Bx(h * 2.8 + col * .8, .4, row * .55, .7, 1.6, .5, stopped ? B.t3 : [DV(5), DV(0), DV(4)][(col + row) % 3], '', nameOf(c), c); })), o, H, { tilt: 30 }); };
  R.models = (d, H, o) => { const rw = rows(d).slice(0, 8); if (!rw.length) return EMPTY('models need rows');
    return isoForm('models', [plate(rw.length * .92 + .2, 2)].concat(rw.map((m, i) => { const p = num(m.params ?? m.b ?? m.size ?? 1); const s = .5 + Math.log10(p + 1) * .5; return Bx(i * .92, .5, 0, s * .9, s * .9, s, m.resident || m.loaded ? B.ac : B.bd2, '', nameOf(m), m); })), o, H); };
  R.activity = (d, H, o) => { const ev = evsOf(d).slice(-18); if (!ev.length) return EMPTY('activity needs events'); const dur = (r) => num(r.duration ?? r.ms ?? r.seconds ?? 0); const hi = Math.max(...ev.map(dur)) || 1;
    return isoForm('activity', [Bx(0, .5, 0, ev.length * .38 + .2, .3, .08, B.bd2)].concat(ev.map((r, i) => Bx(i * .38, .45, .08, .28, .4, .2 + (dur(r) ? dur(r) / hi * .7 : rng(i * 3) * .7), /err|fail/i.test(String(r.kind ?? r.level ?? '')) ? B.ac4 : DV(String(r.kind ?? '').length + i), '', txt(r), r))), o, H); };
  /* every sandbox, up to draw.max (48) - it drew the first six at six fixed places (owner, 2026-09-27: "could actually display
     all the sandboxes - iso elements should be able to scale up to a max number of participant objects"): a grid that fills
     the plate, each block as big as the count allows, coloured by its role, low and grey when it is not running */
  const isoGrid = (n, W0, D0) => { const cols = Math.max(1, Math.ceil(Math.sqrt(n * W0 / D0))), rowsN = Math.max(1, Math.ceil(n / cols)); return { cols, cell: Math.min(W0 / cols, D0 / rowsN) }; };
  R.sandboxes = (d, H, o) => { const all = rows(d); if (!all.length) return EMPTY('sandboxes need rows');
    const rw = all.slice(0, Math.max(1, +(o && o.draw && o.draw.max) || 48)), g = isoGrid(rw.length, 4.8, 2.8), roles = [];
    const roleCol = (r) => { const k = String(r || ''); let i = roles.indexOf(k); if (i < 0) { roles.push(k); i = roles.length - 1; } return DV(i + 2); };
    const blocks = rw.flatMap((s, i) => { const st = String(s.status ?? s.state ?? s.running ?? ''); const idle = s.running === false || /^(idle|paused|stopped|false|no|off)$/i.test(st);
      const sz = g.cell * (idle ? .62 : .78), x = .18 + (i % g.cols) * g.cell + (g.cell - sz) / 2, y = .18 + Math.floor(i / g.cols) * g.cell + (g.cell - sz) / 2, h = Math.max(.2, Math.min(1, g.cell * .9)) * (idle ? .55 : 1);
      return [Bx(x, y, 0, sz, sz, h, idle ? B.t3 : roleCol(s.role), '', nameOf(s), s)].concat(s.pinned ? [Bx(x + sz * .42, y + sz * .42, h, .06, .06, .5 * h + .3, B.t2)] : []); });
    return isoForm('sandboxes', [Bx(0, 0, 0, 5.2, .18, .5, B.bd2), Bx(0, 3, 0, 5.2, .18, .5, B.bd2), Bx(0, 0, 0, .18, 3.2, .5, B.bd2), Bx(5, 0, 0, .18, 3.2, .5, B.bd2), Bx(.18, .18, -.1, 4.8, 2.8, .1, B.s3)].concat(blocks), o, H, { tilt: 30 }); };

  /* ── the ISO FRAME: real markup on the plane — a screen standing on the plate or a sheet lying on it ── */
  const XIF_KIND = { terminal: 'term', term: 'term', page: 'page', notebook: 'nb', panel: 'panel', web: 'web', browser: 'web', chart: 'chart', image: 'img', img: 'img', chat: 'chat', dashboard: 'dash', dash: 'dash', form: 'form', list: 'list' };
  const xifLines = (d, kind) => { const ev = Array.isArray(d) ? d : (d && typeof d === 'object' ? (d.lines || d.items || d.rows || d.messages || []) : String(d == null ? '' : d).split('\n')); return (Array.isArray(ev) ? ev : []).slice(0, 7).map((l) => { if (typeof l === 'string') return { a: l, b: '', c: kind === 'term' && /\$\s*$/.test(l) ? 'cur' : (kind === 'term' && /^\S+[$#]/.test(l) ? 'p' : '') }; if (Array.isArray(l)) return l.length === 2 ? { a: String(l[0] ?? ''), b: '', c: String(l[1] ?? '') } : { a: String(l[0] ?? ''), b: String(l[1] ?? ''), c: String(l[2] ?? '') }; const c = l.cls || l.c || (l.status != null ? 'row ' + (stCol(l.status) === B.ac2 ? 'ok' : stCol(l.status) === B.ac4 ? 'bad' : stCol(l.status) === B.ac3 ? 'warn' : '') : (l.me ? 'msg me' : (kind === 'chat' ? 'msg' : (kind === 'form' ? 'field' + (l.required ? ' lit' : '') : (kind === 'nb' ? (l.output != null ? 'out' : 'code') : ''))))); return { a: String(l.a ?? l.k ?? l.text ?? l.title ?? l.name ?? l.source ?? l.output ?? ''), b: String(l.b ?? l.v ?? l.value ?? l.status ?? l.when ?? ''), c }; }); };
  R.frame = (d, H, o) => { if (!ISO()) return ISO_WAIT; const I = ISO(); const W = wof(o), HH = hof(H); const kind = XIF_KIND[String((o && o.draw && o.draw.kind) || (d && d.kind) || 'terminal').toLowerCase()] || 'term'; const stand = (o && o.draw && o.draw.stand != null) ? !!o.draw.stand : ['term', 'panel', 'web', 'img', 'chat'].includes(kind); const FW = stand ? 170 : 150, FH = stand ? 100 : 112;
    const P1 = I.proj(32, 42, 1, true); const f0 = I.frame(P1, { stand, W: FW, H: FH, col: B.s3, col2: B.bd2, s: 1 }); const s = Math.min((o && o.frameMax) || 3, (W - 14) / f0.bw, (HH - 14) / f0.bh); /* the exploded scene's plane asks for a bigger screen */ const f = I.frame(P1, { stand, W: FW, H: FH, col: B.s3, col2: B.bd2, s, cx: W / 2, cy: HH / 2 });
    const title = String((d && typeof d === 'object' && !Array.isArray(d) && (d.title || d.session || d.url)) || (o && o.title) || kind); const lines = xifLines(d, kind); const bars = (d && Array.isArray(d.bars)) ? d.bars : (kind === 'chart' ? series(d && (d.series || d.values) || []).slice(0, 8).map((v, i, a) => Math.round(v / (Math.max(...a) || 1) * 40) + 'px') : []);
    const panel = kind === 'panel' && d && (d.panel || d.id) ? '<iframe src="' + esc(((o && o.base) || '') + '/ui/panel/window?id=' + encodeURIComponent(String(d.panel || d.id))) + '" style="width:100%;height:100%;border:none;background:transparent"></iframe>' : '';
    const body = panel || ('<div class="xif-bd">' + lines.map((ln) => '<div class="xif-ln ' + esc(ln.c) + '"><span class="k">' + esc(ln.a) + '</span><span class="v">' + esc(ln.b) + '</span></div>').join('') + '<div class="xif-tiles"><i></i><i></i><i></i><i></i><i></i><i></i></div><div class="xif-img"></div><div class="xif-bars">' + bars.map((b) => '<i style="height:' + esc(String(b)) + '"></i>').join('') + '</div></div>');
    return wrap('frame', isow(W, HH, f.faces.map(fpx).join('') + '<div class="xif ' + kind + '" style="left:' + f.x + ';top:' + f.y + ';width:' + f.w + ';height:' + f.h + ';transform:' + f.tf + '"><div class="xif-hd"><i></i><i></i><i></i><span>' + esc(title) + '</span></div>' + body + '</div>')); };
  R['terminal@iso'] = (d, H, o) => R.frame(d, H, Object.assign({}, o, { draw: Object.assign({}, (o && o.draw) || {}, { kind: 'terminal', stand: true }) }));
  R['panel@iso'] = (d, H, o) => R.frame(Object.assign({ kind: 'panel' }, (d && typeof d === 'object') ? d : {}, { panel: (o && o.panel) || (d && d.panel) }), H, Object.assign({}, o, { draw: Object.assign({}, (o && o.draw) || {}, { kind: 'panel', stand: true }) }));

  /* ── the moving forms that are not isometric: orbit, turbine, scope, pulse, comet, split-flap ── */
  R.orbit = (d, H, o) => { const rw = rows(d).slice(0, 12); if (!rw.length) return EMPTY('an orbit needs items'); const pal = palOf(o); const ring = (r) => Math.min(2, num(r.ring ?? r.age ?? r.band ?? 0)); const sz = (r) => num(r.size ?? r.work ?? r.tokens ?? 6);
    const rings = [0, 1, 2].map((ri) => { const dd = 34 + ri * 26; const items = rw.filter((r) => ring(r) === ri); return items.length ? '<span class="oring" style="width:' + dd + '%;height:' + dd + '%;margin:' + (-dd / 2) + '% 0 0 ' + (-dd / 2) + '%;transform:scaleY(.46)"><span class="ospin" style="animation-duration:' + (11 + ri * 6) + 's;animation-direction:' + (ri % 2 ? 'reverse' : 'normal') + '">' + items.map((r, i) => { const a = (r.angle != null ? num(r.angle) : (i / items.length * 360 + ri * 40)) * Math.PI / 180; const s = Math.max(4, Math.min(14, sz(r) * 1.1)); return '<i class="od" style="left:' + (50 + Math.cos(a) * 50).toFixed(1) + '%;top:' + (50 + Math.sin(a) * 50).toFixed(1) + '%;width:' + s + 'px;height:' + s + 'px;background:' + (r.col || pal(i + ri * 3)) + '"><em>' + esc(nameOf(r)) + '</em></i>'; }).join('') + '</span></span>' : ''; });
    return wrap('orbit', '<div class="orbw" style="height:' + hof(H) + 'px">' + rings.join('') + '<i class="ocore"><em>' + rw.length + '</em></i><span class="obig"><b>' + rw.length + '</b><span>' + esc(String((d && d.note) || ((o && o.title) || 'in flight') + ' · 3 age bands')) + '</span></span></div>'); };
  R.turbine = (d, H, o) => { const l = level(d); if (!l) return EMPTY('a turbine needs a rate'); const rate = Math.max(.05, l.v); const dur = (26 / rate).toFixed(2) + 's', durB = (26 / (rate * .62)).toFixed(2) + 's'; const blade = (cls, i) => '<path d="M60,60 C60,38 62,26 70,16 C82,26 86,44 78,58 Z" transform="rotate(' + (i * 51.43).toFixed(1) + ' 60 60)" class="' + cls + '"/>'; const blades = (g) => Array.from({ length: 7 }, (_, i) => blade(i % 2 ? 'bl2' : 'bl', i)).join('');
    const kv = (d && typeof d === 'object' && !Array.isArray(d)) ? Object.keys(d).filter((k) => !['value', 'v', 'unit', 'delta', 'min', 'max', 'trend', 'note', 'col'].includes(k) && typeof d[k] !== 'object').slice(0, 4) : [];
    return wrap('turbine', '<div class="fanw" style="height:' + hof(H) + 'px"><svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="55" fill="none" stroke="' + B.bd + '" stroke-width="1"/><circle cx="60" cy="60" r="47" fill="' + mix(B.ac, 6) + '"/><g class="fanblur" style="animation-duration:' + durB + '">' + blades() + '</g><g class="fanspin" style="animation-duration:' + dur + '">' + blades() + '</g><circle cx="60" cy="60" r="11" fill="' + DK + '" stroke="' + B.bd2 + '"/><circle cx="60" cy="60" r="4" fill="' + B.ac + '"/></svg><div class="fanr"><span class="big">' + esc(fmt(l.v) + ' ' + (l.unit || 'per s')) + '</span>' + kv.map((k) => '<span class="kv">' + esc(k) + '<b>' + esc(String(d[k])) + '</b></span>').join('') + '</div></div>'); };
  R.scope = (d, H, o) => { const v = series(d); if (v.length < 2) return EMPTY('a scope needs a series'); const HH = hof(H); const half = Math.floor(v.length / 2); const cur = v.length > 8 ? v.slice(half) : v, ghost = v.length > 8 ? v.slice(0, half) : []; const lo = Math.min(...v), hi = Math.max(...v); const sorted = cur.slice().sort((a, b) => a - b), q = (f) => fmt(sorted[Math.min(sorted.length - 1, Math.floor(f * sorted.length))]); const unit = (o && o.draw && o.draw.unit) || ''; const col = (d && d.col) || B.ac2;
    return wrap('scope', '<div class="scw" style="height:' + HH + 'px"><svg viewBox="0 0 300 96" preserveAspectRatio="none">' + [24, 48, 72].map((y) => '<line x1="0" y1="' + y + '" x2="300" y2="' + y + '" class="gl"/>').join('') + [60, 120, 180, 240].map((x) => '<line x1="' + x + '" y1="0" x2="' + x + '" y2="96" class="gl"/>').join('') + (ghost.length > 1 ? '<polyline points="' + poly(ghost, 300, 96, 7, lo, hi).join(' ') + '" fill="none" stroke="' + mix(col, 26) + '" stroke-width="1.5" vector-effect="non-scaling-stroke"/>' : '') + '<polyline points="' + poly(cur, 300, 96, 7, lo, hi).join(' ') + '" fill="none" stroke="' + col + '" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg><div class="beam"></div><div class="rd"><span>p50 <b>' + q(.5) + unit + '</b></span><span>p95 <b>' + q(.95) + unit + '</b></span><span>max <b>' + fmt(hi) + unit + '</b></span></div></div>'); };
  R.pulse = (d, H, o) => { const ev = evsOf(d); if (!ev.length) return EMPTY('a pulse needs events'); const by = {}, order = []; ev.forEach((r) => { const k = String(r.kind ?? r.level ?? r.type ?? 'event'); if (!by[k]) { by[k] = 0; order.push(k); } by[k]++; }); const kinds = order.slice(0, 4); const pal = palOf(o); const kc = (k, i) => /err|fail/i.test(k) ? B.ac4 : (i === 0 ? B.ac : pal(i + 1));
    const total = ev.length; const rate = (d && d.rate != null) ? num(d.rate) : total; const rings = kinds.flatMap((k, i) => { const per = Math.max(.8, 6 * (1 - by[k] / total)); return [0, 1].map((j) => '<span class="pr" style="color:' + kc(k, i) + ';animation-delay:' + (-(j * per / 2) - i * .35).toFixed(2) + 's;animation-duration:' + per.toFixed(1) + 's"></span>'); });
    return wrap('pulse', '<div class="plw" style="height:' + hof(H) + 'px">' + rings.join('') + '<span class="phub"></span><span class="prate">' + esc(fmt(rate)) + '<small>' + esc(String((d && d.unit) || 'events' + (d && d.rate != null ? '/s' : ''))) + '</small></span>' + kinds.map((k, i) => '<span class="pk" style="left:' + (i % 2 ? 68 : 4) + '%;top:' + (i < 2 ? 78 : 90) + '%"><i style="background:' + kc(k, i) + '"></i>' + esc(k) + '</span>').join('') + '</div>'); };
  R.comet = (d, H, o) => { const ev = evsOf(d); if (!ev.length) return EMPTY('a comet needs events'); const hrs = (r) => { const m = hhmm(r.when ?? r.t ?? r.ts ?? r.time).match(/(\d{1,2}):(\d{2})/); return m ? +m[1] + (+m[2]) / 60 : 0; }; const at = (h, r) => { const a = (h / 24) * Math.PI * 2 - Math.PI / 2; return [(50 + Math.cos(a) * r).toFixed(1) + '%', (50 + Math.sin(a) * r).toFixed(1) + '%']; }; const cmR = 44; const nowH = new Date().getHours() + new Date().getMinutes() / 60;
    return wrap('comet', '<div class="cmw" style="height:' + hof(H) + 'px"><span class="cmr"><i style="transform:rotate(' + (nowH / 24 * 360).toFixed(1) + 'deg)"></i></span><span class="cmk">' + ev.slice(0, 24).map((e, i) => { const p = at(hrs(e), cmR); const bad = /err|fail/i.test(String(e.kind ?? e.level ?? '')); return '<span class="cmt" style="left:' + p[0] + ';top:' + p[1] + ';background:' + (e.col || (bad ? B.ac4 : DV(String(e.kind ?? '').length + i))) + ';width:' + (bad ? 8 : 5) + 'px;height:' + (bad ? 8 : 5) + 'px" title="' + esc(txt(e)) + '"></span>'; }).join('') + [[0, '00'], [6, '06'], [12, '12'], [18, '18']].map((h) => { const p = at(h[0], cmR + 9); return '<span class="cmh" style="left:' + p[0] + ';top:' + p[1] + '">' + h[1] + '</span>'; }).join('') + '</span><span class="cmc"><b>' + ev.length + '</b><span>' + esc(String((d && d.note) || 'events · last 24h')) + '</span></span></div>'); };
  R['split-flap'] = (d, H, o) => { const src = (d && typeof d === 'object' && !Array.isArray(d)) ? d : { text: Array.isArray(d) ? d.join('\n') : String(d == null ? '' : d) }; const lines = (Array.isArray(src.lines) ? src.lines : String(src.text ?? src.value ?? '').split('\n')).slice(0, (o && o.draw && o.draw.lines) || 2); if (!lines.length || !lines.some((l) => String(l).trim())) return EMPTY('a split-flap needs a line of text');
    const hi = Array.isArray(src.highlight) ? src.highlight : null; const flap = (s, li) => '<div class="flap">' + String(s).toUpperCase().slice(0, 28).split('').map((ch, i) => '<span class="' + (ch === ' ' ? 'sp' : '') + (hi && li === 0 && i >= hi[0] && i < hi[1] ? ' hi' : '') + '" style="--i:' + i + '">' + esc(ch === ' ' ? ' ' : ch) + '</span>').join('') + '</div>';
    return wrap('split-flap', '<div class="flapw">' + lines.map(flap).join('') + (src.note ? '<div class="flapl"><span>' + esc(String(src.note)) + '</span></div>' : '') + '</div>'); };
  R.ticker = (d, H, o) => R.counter(d, H, o);
  R.topology = (d, H, o) => R.graph(d, H, o);   // flat: the node graph; iso: the floors and pipes

  /* ══ CAPABILITY OUTPUT WIDGETS (the widget review, round 2: "we need to better define good widgets for cap outputs and
     make caps output to widgets and have a broad set of widgets to display results and streams of operation") ═══════
     The result forms - what a capability's ANSWER is drawn as, wherever it lands (a chat reply, a canvas item, a
     dashboard tile, a deep dive):
       kv        a record: its fields, name beside value                  (existing)
       table     rows of the same shape: sortable, searchable, paged       (existing)
       list      a list of plain things                                    (existing)
       json      anything else with structure: a tree you open             (new)
       log       a stream of lines, newest in view (follow)                (existing; fromCapStream appends)
       terminal  a command and what it printed                             (existing)
       diff      a patch: files, hunks, additions and removals coloured    (new)
       code      source with line numbers and its language                 (new)
       progress  steps with their state (done · running · failed · waiting), how far, how long   (new)
       hero      a number with its trend                                   (existing)
       trace · area · column   a series; several series stacked; counts    (existing)
       status    a health answer: its verdict large, its checks under it   (new)
       files     paths (a tree of files)                                   (existing)
       media     an image, a video, a sound - or several                   (new)
       error     a failed answer: what failed, said once, the detail folded (new)
       markdown  prose a cap wrote: headings, lists, code, emphasis        (new)
     Each is drawn by VeraWidget.draw like every other form, and has a sample face. */
  const CAP_FORMS = ['kv', 'table', 'list', 'json', 'log', 'terminal', 'diff', 'code', 'progress', 'hero', 'trace', 'area', 'column', 'status', 'files', 'media', 'error', 'markdown'];
  const isObj = (x) => !!x && typeof x === 'object' && !Array.isArray(x);
  const isScalar = (v) => v == null || typeof v !== 'object';
  // json: a tree you open - objects and arrays fold (the first level open), scalars coloured by kind, big ones capped
  R.json = (d, H, o) => {
    if (d === undefined) return EMPTY('nothing to show'); let budget = 400;
    const leaf = (v) => v === null ? '<i class="jn">null</i>' : typeof v === 'number' ? '<i class="jnum">' + esc(String(v)) + '</i>' : typeof v === 'boolean' ? '<i class="jb">' + v + '</i>' : '<i class="js">"' + esc(String(v).slice(0, 300)) + (String(v).length > 300 ? '…' : '') + '"</i>';
    const node = (k, v, depth) => { if (--budget < 0) return ''; const key = k == null ? '' : '<b>' + esc(String(k)) + '</b>';
      if (!v || typeof v !== 'object') return '<div class="jl">' + key + leaf(v) + '</div>';
      const arr = Array.isArray(v), ks = arr ? v.map((_, i) => i) : Object.keys(v), n = ks.length;
      const kids = ks.slice(0, 200).map((kk) => node(arr ? kk : kk, v[kk], depth + 1)).join('') + (n > 200 ? '<div class="jl"><i class="jn">… ' + (n - 200) + ' more</i></div>' : '');
      return '<details' + (depth < 1 ? ' open' : '') + '><summary>' + key + '<i class="jt">' + (arr ? '[' + n + ']' : '{' + n + '}') + '</i></summary>' + kids + '</details>'; };
    return wrap('json', '<div class="vb-json">' + node(null, d, 0) + '</div>');
  };
  // diff: the patch's files and hunks, + and - coloured, a count of each at the head
  R.diff = (d, H, o) => {
    const txt0 = typeof d === 'string' ? d : (isObj(d) ? String(d.diff ?? d.patch ?? d.text ?? '') : ''); if (!txt0.trim()) return EMPTY('a diff needs a patch');
    const L = txt0.split('\n'); const add = L.filter((l) => /^\+(?!\+\+)/.test(l)).length, del = L.filter((l) => /^-(?!--)/.test(l)).length, files = L.filter((l) => /^diff --git|^\+\+\+ /.test(l)).length;
    const cls = (l) => /^(diff --git|index |\+\+\+ |--- )/.test(l) ? 'df' : /^@@/.test(l) ? 'dh' : /^\+/.test(l) ? 'da' : /^-/.test(l) ? 'dd' : '';
    return wrap('diff', '<div class="vb-dhd"><b class="da">+' + add + '</b><b class="dd">−' + del + '</b><span>' + (files ? Math.ceil(files / 2) + ' file' + (files > 2 ? 's' : '') : '') + '</span></div><pre class="vb-code vb-diff">' + L.slice(0, 1500).map((l) => '<span class="' + cls(l) + '">' + esc(l) + '</span>').join('\n') + '</pre>');
  };
  // code: numbered lines and the language
  R.code = (d, H, o) => {
    const code = typeof d === 'string' ? d : (isObj(d) ? String(d.code ?? d.content ?? d.source ?? d.text ?? '') : ''); if (!code.trim()) return EMPTY('code needs source');
    const path = isObj(d) ? String(d.path ?? d.filename ?? d.file ?? '') : ''; const lang = (isObj(d) && (d.lang || d.language)) || ((o && o.draw && o.draw.lang) || (path.match(/\.([a-z0-9]+)$/i) || [])[1] || '');
    const L = code.split('\n');
    return wrap('code', '<div class="vb-dhd"><span>' + esc(path ? path.split('/').pop() : 'code') + '</span><span>' + esc(String(lang)) + ' · ' + L.length + ' lines</span></div><pre class="vb-code">' + L.slice(0, 2000).map((l, i) => '<span><u>' + (i + 1) + '</u>' + esc(l) + '</span>').join('\n') + '</pre>');
  };
  // progress: every step with its state, how long it took; the bar is done of all
  const stepState = (s) => { const k = String(s.status ?? s.state ?? (s.done ? 'done' : (s.current || s.now ? 'running' : ''))).toLowerCase(); return /fail|error|crash|abort/.test(k) ? 'failed' : /done|ok|pass|success|complete|finish|merged/.test(k) ? 'done' : /run|active|current|now|progress|work/.test(k) ? 'running' : /skip/.test(k) ? 'skipped' : 'waiting'; };
  R.progress = (d, H, o) => {
    const st = isObj(d) && Array.isArray(d.steps || d.stages || d.items || d.events) ? (d.steps || d.stages || d.items || d.events) : (Array.isArray(d) ? d : []);
    const rw = st.map((s) => (s && typeof s === 'object') ? s : { name: String(s) }); if (!rw.length) return EMPTY('progress needs steps');
    const S = rw.map(stepState), done = S.filter((x) => x === 'done' || x === 'skipped').length, failed = S.includes('failed'), ic = { done: '✓', running: '●', failed: '✗', waiting: '○', skipped: '–' };
    const dur = (s) => { const v = s.elapsed_s ?? s.duration_s ?? s.seconds ?? (s.elapsed_ms != null ? s.elapsed_ms / 1000 : (s.ms != null ? s.ms / 1000 : null)); return v == null ? '' : (v >= 60 ? Math.round(v / 60) + 'm' : (Math.round(num(v) * 10) / 10) + 's'); };
    const lim = Math.max(3, Math.floor(((H || 120) - 24) / 20)), cur = Math.max(0, S.indexOf('running')), from = Math.max(0, Math.min(rw.length - lim, cur - 1));
    return wrap('progress', '<div class="vb-pgb"><i style="width:' + (done / rw.length * 100).toFixed(1) + '%;background:' + (failed ? B.ac4 : B.ac2) + '"></i></div><span class="vb-lbl">' + done + ' of ' + rw.length + (failed ? ' · failed at ' + esc(nameOf(rw[S.indexOf('failed')]) || 'a step') : (S.includes('running') ? ' · ' + esc(nameOf(rw[cur]) || 'running') : '')) + '</span>'
      + rw.slice(from, from + lim).map((s, j) => { const k = S[from + j]; return '<span class="vb-ps ' + k + '"' + itemAttr(s, 'step') + ' data-tip="' + esc(rowTip(s, nameOf(s) || String(s.step ?? s.stage ?? ''))) + '"><i>' + ic[k] + '</i><em>' + esc(String(nameOf(s) || s.step || s.stage || s.text || s.message || '')) + '</em><small>' + esc(String(s.detail ?? s.message ?? s.note ?? '').slice(0, 80)) + '</small><b>' + esc(dur(s)) + '</b></span>'; }).join(''));
  };
  // status: the verdict large, the checks under it
  const verdictOf = (d) => { if (!isObj(d)) return String(d ?? ''); const v = d.status ?? d.level ?? d.state ?? d.health ?? (d.ok === true ? 'ok' : d.ok === false ? 'failed' : (d.healthy === true ? 'healthy' : d.healthy === false ? 'unhealthy' : '')); return String(v ?? ''); };
  const checksOf = (d) => { if (!isObj(d)) return []; const c = d.checks || d.findings || d.components || d.services || d.results || d.backends; if (Array.isArray(c)) return c.filter(isObj);
    if (isObj(c)) return Object.keys(c).map((k) => isObj(c[k]) ? Object.assign({ name: k }, c[k]) : { name: k, status: c[k] });
    return Object.keys(d).filter((k) => typeof d[k] === 'boolean' || (typeof d[k] === 'string' && /^(ok|up|down|err|error|warn|healthy|unhealthy|running|stopped|serving|failed|pass|fail)$/i.test(d[k]))).filter((k) => !/^(ok|status|state|level|health|healthy)$/.test(k)).map((k) => ({ name: k, status: d[k] })); };
  R.status = (d, H, o) => {
    const v = verdictOf(d); const ck = checksOf(d); if (!v && !ck.length) return EMPTY('a status needs a verdict or checks');
    const col = stCol(v || (ck.every((c) => stCol(c.status ?? c.state ?? c.severity ?? c.ok) === B.ac2) ? 'ok' : 'warn'));
    const msg = isObj(d) ? String(d.message ?? d.summary ?? d.detail ?? d.reason ?? '') : '';
    const lim = Math.max(2, Math.floor(((H || 120) - 48) / 18));
    return wrap('status', '<div class="vb-stv"><i style="background:' + col + '"></i><b style="color:' + col + '">' + esc(v || (ck.length + ' checks')) + '</b>' + (msg ? '<span>' + esc(msg.slice(0, 140)) + '</span>' : '') + '</div>'
      + ck.slice(0, lim).map((c) => { const s = c.status ?? c.state ?? c.severity ?? c.ok ?? ''; return '<span class="vb-stc"' + itemAttr(c, 'check') + ' data-tip="' + esc(rowTip(c)) + '"><i style="background:' + stCol(s) + '"></i><em>' + esc(nameOf(c) || String(c.message ?? '').slice(0, 40)) + '</em><small>' + esc(String(typeof s === 'boolean' ? (s ? 'ok' : 'no') : s)) + '</small></span>'; }).join('') + (ck.length > lim ? '<span class="vb-lbl">+ ' + (ck.length - lim) + ' more</span>' : ''));
  };
  // media: an image (an address or base64), a video, a sound - the first large, the count of the rest
  const mediaOf = (x) => { if (!x) return null; if (typeof x === 'string') return /^data:|^https?:|^\//.test(x) ? { url: x } : null; if (!isObj(x)) return null;
    const b = x.image_b64 || x.b64 || x.base64; if (b) { const s = String(b); const mime = /^\/9j\//.test(s) ? 'image/jpeg' : /^R0lGOD/.test(s) ? 'image/gif' : /^UklGR/.test(s) ? 'image/webp' : 'image/png'; return { url: 'data:' + mime + ';base64,' + s, alt: x.title || x.caption || '' }; }
    const u = x.url || x.src || x.image || x.path || x.file; return (u && /^(data:(image|video|audio)\/|https?:|\/)/i.test(String(u))) ? { url: String(u), alt: String(x.title || x.caption || x.name || ''), kind: x.kind || x.mime || '' } : null; };
  R.media = (d, H, o) => {
    const list = Array.isArray(d) ? d : (isObj(d) && Array.isArray(d.images || d.media || d.files || d.items) ? (d.images || d.media || d.files || d.items) : [d]);
    const M = list.map(mediaOf).filter(Boolean); if (!M.length) return EMPTY('media needs an image, a video or a sound');
    const m = M[0], u = m.url, k = String(m.kind || '') + ' ' + u; const h = Math.max(60, (H || 120) - 18);
    const el = /video|\.(mp4|webm|mov)(\?|$)/i.test(k) ? '<video src="' + esc(u) + '" controls style="max-height:' + h + 'px"></video>' : /audio|\.(mp3|wav|ogg|m4a)(\?|$)/i.test(k) ? '<audio src="' + esc(u) + '" controls></audio>' : '<img src="' + esc(u) + '" alt="' + esc(m.alt || '') + '" style="max-height:' + h + 'px" loading="lazy">';
    return wrap('media', '<div class="vb-media">' + el + '</div>' + cap(esc(String(m.alt || '')) + (M.length > 1 ? ' · + ' + (M.length - 1) + ' more' : '')));
  };
  // error: what failed, said once; the detail (a trace, a stderr) folded under it
  R.error = (d, H, o) => {
    const msg = typeof d === 'string' ? d : (isObj(d) ? String(d.error ?? d.message ?? d.detail ?? d.reason ?? 'failed') : 'failed'); const det = isObj(d) ? String(d.traceback ?? d.trace ?? d.stderr ?? d.stack ?? (d.detail !== msg ? d.detail ?? '' : '')) : '';
    const who = (o && o.record && o.record.source) || (isObj(d) && d.capability) || '';
    return wrap('error', '<div class="vb-err"><i>✗</i><div><b>' + esc(who ? who + ' failed' : 'failed') + '</b><span>' + esc(msg.slice(0, 600)) + '</span></div></div>' + (det ? '<details class="vb-errd"><summary>detail</summary><pre>' + esc(det.slice(0, 8000)) + '</pre></details>' : ''));
  };
  // markdown: prose a capability wrote - headings, lists, fenced code, emphasis, links (escaped first, then marked up)
  const md = (s) => { const out = []; let inCode = false, list = '';
    const inl = (t) => esc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|\W)\*([^*]+)\*(?=\W|$)/g, '$1<em>$2</em>').replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    String(s).split('\n').forEach((l) => { if (/^```/.test(l)) { if (list) { out.push('</' + list + '>'); list = ''; } out.push(inCode ? '</pre>' : '<pre>'); inCode = !inCode; return; }
      if (inCode) { out.push(esc(l)); return; }
      const h = l.match(/^(#{1,4})\s+(.*)$/), li = l.match(/^\s*[-*]\s+(.*)$/), ol = l.match(/^\s*\d+[.)]\s+(.*)$/);
      if ((li || ol) && list !== (li ? 'ul' : 'ol')) { if (list) out.push('</' + list + '>'); list = li ? 'ul' : 'ol'; out.push('<' + list + '>'); }
      if (!(li || ol) && list) { out.push('</' + list + '>'); list = ''; }
      if (h) out.push('<h' + (h[1].length + 2) + '>' + inl(h[2]) + '</h' + (h[1].length + 2) + '>'); else if (li || ol) out.push('<li>' + inl((li || ol)[1]) + '</li>'); else if (l.trim()) out.push('<p>' + inl(l) + '</p>'); });
    if (list) out.push('</' + list + '>'); if (inCode) out.push('</pre>'); return out.join(''); };
  R.markdown = (d, H, o) => { const s = typeof d === 'string' ? d : (isObj(d) ? String(d.markdown ?? d.md ?? d.report ?? d.summary ?? d.text ?? d.content ?? '') : ''); if (!s.trim()) return EMPTY('nothing written'); return wrap('markdown', '<div class="vb-md">' + md(s.slice(0, 40000)) + '</div>'); };
  const CAPOUT_CSS = '.vb-json{font-family:var(--b-mono);font-size:11px;line-height:1.5;overflow:auto;min-height:0;flex:1}.vb-json details{padding-left:12px}.vb-json > details{padding-left:0}.vb-json summary{cursor:pointer;list-style:none}.vb-json summary::before{content:"▸ ";color:var(--b-t3)}.vb-json details[open] > summary::before{content:"▾ "}'
    + '.vb-json b{font-weight:400;color:var(--b-t2);margin-right:6px}.vb-json b::after{content:":"}.vb-json .jl{padding-left:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-json i{font-style:normal}.vb-json .js{color:var(--b-ac2)}.vb-json .jnum{color:var(--b-ac)}.vb-json .jb{color:var(--b-ac3)}.vb-json .jn,.vb-json .jt{color:var(--b-t3);margin-left:4px}'
    + '.vb-code{margin:0;flex:1;min-height:0;overflow:auto;font-family:var(--b-mono);font-size:11px;line-height:1.55;color:var(--b-t1);background:var(--b-s2);border-radius:var(--b-r);padding:6px 8px;white-space:pre;tab-size:2}.vb-code u{display:inline-block;width:3.2em;text-decoration:none;color:var(--b-t3);text-align:right;margin-right:10px;user-select:none}'
    + '.vb-diff .da{color:var(--b-ac2);background:' + 'color-mix(in srgb,var(--b-ac2) 10%,transparent)' + '}.vb-diff .dd{color:var(--b-ac4);background:color-mix(in srgb,var(--b-ac4) 10%,transparent)}.vb-diff .dh{color:var(--b-ac)}.vb-diff .df{color:var(--b-t1);font-weight:600}.vb-diff span{display:inline-block;min-width:100%}'
    + '.vb-dhd{display:flex;gap:10px;align-items:center;font-family:var(--b-mono);font-size:10.5px;color:var(--b-t2)}.vb-dhd .da{color:var(--b-ac2)}.vb-dhd .dd{color:var(--b-ac4)}.vb-dhd span:last-child{margin-left:auto}'
    + '.vb-pgb{height:6px;border-radius:3px;background:var(--b-s3);overflow:hidden;flex:none}.vb-pgb i{display:block;height:100%;border-radius:3px}'
    + '.vb-ps{display:grid;grid-template-columns:16px minmax(0,auto) minmax(0,1fr) auto;gap:8px;align-items:baseline;font-size:11px;line-height:1.35}.vb-ps i{font-style:normal;text-align:center}.vb-ps em{font-style:normal;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ps small{color:var(--b-t3);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ps b{font-family:var(--b-mono);font-weight:400;font-size:10px;color:var(--b-t3)}'
    + '.vb-ps.done i{color:var(--b-ac2)}.vb-ps.failed i,.vb-ps.failed em{color:var(--b-ac4)}.vb-ps.running i{color:var(--b-ac);animation:vw-kread 1.2s ease-in-out infinite}.vb-ps.running em{color:var(--b-t1);font-weight:600}.vb-ps.waiting{color:var(--b-t3)}'
    + '.vb-stv{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.vb-stv > i{width:12px;height:12px;border-radius:50%;box-shadow:0 0 0 4px color-mix(in srgb,currentColor 12%,transparent)}.vb-stv b{font-size:18px;font-weight:700;text-transform:capitalize}.vb-stv span{font-size:11px;color:var(--b-t2);flex-basis:100%}'
    + '.vb-stc{display:grid;grid-template-columns:8px minmax(0,1fr) auto;gap:8px;align-items:center;font-size:11px}.vb-stc i{width:7px;height:7px;border-radius:50%}.vb-stc em{font-style:normal;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-stc small{font-family:var(--b-mono);font-size:10px;color:var(--b-t2)}'
    + '.vb-media{flex:1;min-height:0;display:flex;align-items:center;justify-content:center}.vb-media img,.vb-media video{max-width:100%;object-fit:contain;border-radius:var(--b-r)}.vb-media audio{width:100%}'
    + '.vb-err{display:flex;gap:10px;align-items:flex-start;padding:8px 10px;border-radius:var(--b-r);background:color-mix(in srgb,var(--b-ac4) 10%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--b-ac4) 35%,transparent)}.vb-err > i{font-style:normal;color:var(--b-ac4);font-size:16px;line-height:1}.vb-err b{display:block;color:var(--b-ac4);font-size:12px}.vb-err span{font-size:11px;color:var(--b-t1);white-space:pre-wrap;word-break:break-word}'
    + '.vb-errd summary{cursor:pointer;font-size:10.5px;color:var(--b-t2)}.vb-errd pre{margin:4px 0 0;max-height:200px;overflow:auto;font-family:var(--b-mono);font-size:10.5px;color:var(--b-t2);white-space:pre-wrap}'
    + '.vb-md{flex:1;min-height:0;overflow:auto;font-size:12px;line-height:1.55;color:var(--b-t1)}.vb-md h3,.vb-md h4,.vb-md h5,.vb-md h6{margin:6px 0 3px;font-size:13px}.vb-md p{margin:0 0 6px}.vb-md ul,.vb-md ol{margin:0 0 6px;padding-left:18px}.vb-md code{font-family:var(--b-mono);font-size:11px;background:var(--b-s3);padding:0 4px;border-radius:3px}.vb-md pre{font-family:var(--b-mono);font-size:11px;background:var(--b-s2);padding:6px 8px;border-radius:var(--b-r);overflow:auto}.vb-md a{color:var(--b-ac)}';

  /* ── THE ONE MAPPING: a capability's answer → the widget record(s) that draw it ─────────────────────────────────────
     VeraWidget.fromCapResult(capName, result, opts?) → [record, ...]   (best first; [] for nothing to draw)
       capName  the capability that answered ('' when unknown) - it names the source and picks a per-cap hint
       result   what it answered: the capability's content (an MCP envelope {type:'tool_result', content} is opened)
       opts     { args: the call's arguments (kept as read.args so the widget can read again), title, size,
                  max: how many records at most (default 3) }
     Every record is a full widget record: { id, form, title, source: capName, read: { args, map }, frame: { size },
     data: <the part of the answer it draws>, why: '<the rule that chose it>' } - it draws at once (it carries its data)
     and, placed where the source is readable, refreshes on its own. The first record is the answer's best drawing; the
     ones after it are companions (a chart beside a table of numbers, the checks beside a verdict).
     The rules, in order (the first that matches decides the first record):
       1 error      {ok:false, error} · {error:"..."} with little else            → error
       2 terminal   {command, stdout|stderr}                                      → terminal
       3 media      image_b64 · an image/video/audio url · {images:[...]}         → media
       4 diff       {diff} · {patch} · a string that is a unified diff           → diff
       5 code       {code} · {content, path}                                       → code
       6 hint       the capability's own hint (CAP_HINTS: the dashboard's reads)  → its form and map
       7 progress   {steps|stages:[{name, status}]} · rows of step + state        → progress
       8 events     rows with a time and a line of text                           → log
       9 series     rows with a time and numbers · {history|samples:[...]}        → trace (one number) · area (several)
      10 level      {value, max} → ring · {value} → hero (with its trend when the answer has one)
      11 files      rows with a path                                              → files
      12 status     {status|ok|healthy|level, + checks|findings|components}       → status (+ its checks as a table)
      13 table      rows of the same shape                                        → table (+ ranked when a row has a name and a number)
      14 list       a list of plain things                                        → list
      15 numbers    {name: number, ...}                                           → numbers (≤ 6) · ranked
      16 prose      report · markdown · summary · a long text                     → markdown
      17 record     a flat object of up to 24 plain fields                        → kv
      18 json       anything else with structure                                  → json
      19 string     a short text                                                  → string
     The python catalogue mirrors it (widget_cap_output.py, widget.from_result) so an agent can ask for the same answer. */
  const CAP_HINTS = {
    'sysmon.history': { form: 'trace', map: { series: 'samples', v: 'cpu' } }, 'sysmon.status': { form: 'numbers', map: { pick: { cpu: 'resources.cpu', memory: 'resources.mem', guests: 'proxmox.running', containers: 'docker.running' } } },
    'obs.events': { form: 'log', map: { events: '$', t: 'ts', kind: 'type', text: 'name' } }, 'ollama.request_log': { form: 'log', map: { events: 'entries', t: 'ts', kind: 'instance', text: 'model' } },
    'ollama.route_stats': { form: 'ranked', map: { values: 'stats', count: 'model', sum: 'n' } }, 'obs.node_temps': { form: 'temps', map: { values: 'hosts', name: 'label', value: 'max_c' } },
    'evolve.activity': { form: 'area', map: { series: 'buckets', split: ['pass', 'fail'], t: 'hour' } }, 'evolve.pipeline.list': { form: 'status-matrix' },
    'evolve.unittest.history': { form: 'race-green' }, 'perf.stalls': { form: 'column', map: { values: 'events', value: 'stalled_ms', reverse: true } },
    'syslog.errors': { form: 'log', map: { events: 'warnings', t: 'ts', kind: 'cap_group', text: 'message' } }, 'dash.health.summary': { form: 'pills', map: { values: '$', entries: 'status' } },
    'obs.modules': { form: 'treemap', map: { parts: 'modules', name: 'name', value: 'caps_added' } }, 'estate.health': { form: 'status' }, 'perf.scan': { form: 'status' },
    'obs.health': { form: 'status' }, 'backup.status': { form: 'table', map: { rows: 'guests' }, draw: { columns: ['name', 'status', 'state', 'backups'] } }, 'docker.ps': { form: 'containers', map: { rows: 'containers', name: 'Names', status: 'State', host: 'host_id' } },
    'evolve.sandbox.list': { form: 'sandboxes', map: { rows: 'sandboxes', name: 'name', status: 'running' } }, 'dream.history': { form: 'table', map: { rows: 'history' }, draw: { columns: ['label', 'title', 'started_at', 'signal'] } },
    'fabric.graphs.snapshot': { form: 'vgraph' }, 'fabric.entity_graph.snapshot': { form: 'vgraph' }, 'memory.graph_full': { form: 'vgraph' }, 'topology.snapshot': { form: 'vgraph' }, 'mesh.topology': { form: 'vgraph' },
    'cal.events.list': { form: 'schedule' }, 'exec.bash.run': { form: 'terminal' }, 'code.read': { form: 'code' }, 'code.diff': { form: 'diff' }, 'evolve.pipeline.diff': { form: 'diff' }, 'evolve.sandbox.diff': { form: 'diff' },
    // the Loop Lab's pictures: ci.* (code work), loop.ci.* (the agentic loop), and the Loop Lab's own capabilities
    'ci.matrix': { form: 'status-matrix' }, 'ci.race': { form: 'race-green' }, 'ci.tests': { form: 'test-grid' }, 'ci.board': { form: 'ci-board' }, 'ci.track': { form: 'run-track' },
    'ci.compare': { form: 'run-compare' }, 'ci.pulse': { form: 'ci-pulse' }, 'ci.fleet': { form: 'ci-fleet' }, 'ci.run': { form: 'ci-run' }, 'ci.census': { form: 'census-commits' }, 'loop.ci.perf': { form: 'loop-perf' }, 'census.live': { form: 'census-live' },
    'loop.ci.matrix': { form: 'status-matrix' }, 'loop.ci.race': { form: 'race-green' }, 'loop.ci.board': { form: 'ci-board' },
    'evolve.pipeline.get': { form: 'run-track' }, 'evolve.tasks.overview': { form: 'status-matrix' }, 'workshop.agent_loop.trace': { form: 'status-matrix' }, 'board.items': { form: 'ci-board' } };
  const hintOf = (cap) => { const n = String(cap || ''); if (CAP_HINTS[n]) return CAP_HINTS[n]; if (/\.(diff|patch)$/.test(n)) return { form: 'diff' }; if (/\.(health|healthz)$/.test(n)) return { form: 'status' }; return null; };
  const rowsOfAny = (c) => { if (Array.isArray(c)) return c; if (!isObj(c)) return null; const ok = (v) => Array.isArray(v) && v.length && isObj(v[0]);
    for (const k of ['data', 'result', 'items', 'rows', 'results', 'entries', 'events', 'points', 'series', 'values']) if (ok(c[k])) return c[k]; for (const k of Object.keys(c)) if (ok(c[k])) return c[k]; return null; };
  const rowsKey = (c) => { if (!isObj(c)) return '$'; for (const k of ['data', 'result', 'items', 'rows', 'results', 'entries', 'events', 'points', 'series', 'values']) if (Array.isArray(c[k]) && c[k].length && isObj(c[k][0])) return k; for (const k of Object.keys(c)) if (Array.isArray(c[k]) && c[k].length && isObj(c[k][0])) return k; return '$'; };
  const TIMEK = ['t', 'ts', 'time', 'when', 'at', 'timestamp', 'created_at', 'started_at', 'hour', 'date'], TEXTK = ['text', 'msg', 'message', 'line', 'event', 'summary', 'title'];
  const firstKey = (r, ks) => ks.find((k) => r[k] != null && r[k] !== '');
  const numKeys = (r) => Object.keys(r).filter((k) => typeof r[k] === 'number' && !/^(id|pid|port|vmid|index|idx|i|n_?id)$/i.test(k) && !TIMEK.includes(k));
  const isDiffText = (s) => typeof s === 'string' && /^(diff --git |--- |\+\+\+ |@@ )/m.test(s) && /^[+-]/m.test(s);
  const isMediaUrl = (s) => typeof s === 'string' && /^(data:image\/|data:video\/|https?:.*\.(png|jpe?g|gif|webp|svg|mp4|webm|mov|mp3|wav|ogg)(\?|$))/i.test(s);
  function fromCapResult(capName, result, opts) {
    opts = opts || {}; let c = result;
    if (isObj(c) && c.type === 'tool_result' && 'content' in c) c = c.content;
    if (c === undefined || c === null || c === '') return [];
    const cap = String(capName || ''), max = Math.max(1, +opts.max || 3), title = opts.title || cap || 'result', out = [];
    const mk = (form, data, why, extra) => { const r = Object.assign({ id: (cap || 'result').replace(/[^a-z0-9]+/gi, '-') + '-' + form, form, title: (extra && extra.title) || title, source: cap, read: { args: opts.args || {}, map: (extra && extra.map) || {} }, frame: { size: opts.size || 'l' }, data, why }, extra && extra.draw ? { draw: extra.draw } : {}); out.push(r); return r; };
    const done = () => out.slice(0, max);
    // 1 error
    if (isObj(c) && ((c.ok === false && (typeof c.error === 'string' || typeof c.message === 'string')) || (typeof c.error === 'string' && c.error && Object.keys(c).length <= 3))) { mk('error', c, 'a failed answer: ok false, or an error and little else'); return done(); }
    // 2 terminal
    if (isObj(c) && (c.stdout != null || c.stderr != null) && (c.command != null || c.cmd != null || c.rc != null || c.returncode != null)) { const lines = [c.command || c.cmd ? '$ ' + String(c.command || c.cmd) : ''].concat(String(c.stdout || '').split('\n')).concat(String(c.stderr || '').split('\n')).filter((l, i) => i > 0 || l);
      mk('terminal', { lines, state: c.rc != null ? 'rc ' + c.rc : (c.returncode != null ? 'rc ' + c.returncode : '') }, 'a command and what it printed'); if (c.rc && c.rc !== 0 && c.stderr) mk('error', { error: String(c.stderr).split('\n').filter(Boolean).slice(-1)[0] || 'rc ' + c.rc, stderr: c.stderr }, 'the command failed'); return done(); }
    // 3 media
    if (isMediaUrl(c) || (isObj(c) && (c.image_b64 || c.b64 || isMediaUrl(c.url) || isMediaUrl(c.src) || isMediaUrl(c.image) || (Array.isArray(c.images) && c.images.length)))) { mk('media', c, 'an image, a video or a sound'); return done(); }
    // 4 diff · 5 code
    if (isDiffText(c) || (isObj(c) && (isDiffText(c.diff) || isDiffText(c.patch) || (typeof c.diff === 'string' && c.diff.trim())))) { mk('diff', c, 'a patch'); return done(); }
    if (isObj(c) && ((typeof c.code === 'string' && c.code.trim()) || (typeof c.content === 'string' && c.content.trim() && (c.path || c.filename || c.file)))) { mk('code', c, 'source: code, or a file\'s content'); return done(); }
    // 6 the capability's own hint
    const h = hintOf(cap); if (h) { const r = mk(h.form, c, 'the ' + cap + ' hint', { map: h.map, draw: h.draw }); const drawn = mapped(r, h.form, c); if (!isEmpty(dataFor(drawn, h.form))) { if (!CI_FORMS.test(h.form)) rowsCompanion(c, r); return done(); } out.pop(); }
    const rw = rowsOfAny(c), rk = rowsKey(c);
    // 7 progress
    if (isObj(c) && Array.isArray(c.steps || c.stages) && (c.steps || c.stages).length) { mk('progress', c, 'steps with their state'); return done(); }
    if (rw && rw.length && rw.every((r) => isObj(r) && (r.step != null || r.stage != null || r.phase != null) && (r.status != null || r.state != null || r.done != null))) { mk('progress', { steps: rw }, 'rows of step and state'); return done(); }
    if (rw && rw.length && isObj(rw[0])) {
      const r0 = rw[0], tk = firstKey(r0, TIMEK), xk = firstKey(r0, TEXTK), nk = numKeys(r0);
      // 8 events
      if (tk && xk && rw.every((r) => isObj(r))) { mk('log', c, 'rows with a time and a line of text', { map: { events: rk, t: tk, text: xk, kind: firstKey(r0, ['kind', 'level', 'type', 'severity', 'source']) || '' } }); if (rw.length > 1 && rw.some((r) => /err|fail|warn/i.test(String(r.level ?? r.kind ?? r.severity ?? '')))) mk('pareto', c, 'the lines counted by kind', { map: { values: rk, count: firstKey(r0, ['kind', 'level', 'type', 'severity']) || 'kind' }, title: title + ' · by kind' }); return done(); }
      // 9 series
      if (tk && nk.length && rw.length >= 3 && Object.keys(r0).filter((k) => typeof r0[k] !== 'object').length <= nk.length + 3) { if (nk.length === 1) mk('trace', c, 'a series: a time and a number', { map: { series: rk, t: tk, v: nk[0] } }); else mk('area', c, 'series: a time and ' + nk.length + ' numbers', { map: { series: rk, t: tk, split: nk.slice(0, 4) } }); mk('table', c, 'the readings', { map: { rows: rk }, title: title + ' · readings' }); return done(); }
      // 11 files
      if (rw.every((r) => isObj(r) && (r.path != null || r.file != null))) { mk('files', c, 'rows with a path', { map: { rows: rk } }); return done(); }
    }
    // 10 level
    if (isObj(c) && typeof c.value === 'number') { const tr = Array.isArray(c.history || c.trend || c.series) ? (c.history || c.trend || c.series) : null; if (c.max != null && !tr) mk('ring', c, 'a value of a maximum'); else mk('hero', c, 'a value' + (tr ? ' with its trend' : '')); return done(); }
    // 12 status
    // 16 prose first: {ok:true, report:"..."} is a report that succeeded, not a verdict
    if (isObj(c)) { const pk = ['report', 'markdown', 'md', 'summary', 'text', 'answer', 'content'].find((k) => typeof c[k] === 'string' && c[k].trim()); if (pk && (c[pk].length >= 200 || /^#|\n[-*] |\n\n/.test(c[pk]))) { mk('markdown', c[pk], 'prose: ' + pk); const rest = Object.keys(c).filter((k) => k !== pk && isScalar(c[k])); if (rest.length > 1) mk('kv', c, 'the rest of the answer', { map: { keys: rest.slice(0, 12) }, title: title + ' · fields' }); return done(); } }
    const hasVerdict = isObj(c) && ['status', 'state', 'level', 'health', 'healthy'].some((k) => c[k] != null && isScalar(c[k]));
    if (isObj(c) && verdictOf(c) && ((hasVerdict && checksOf(c).length) || (hasVerdict && /^(ok|up|down|healthy|unhealthy|degraded|warn|error|failed|serving|running|stopped)$/i.test(verdictOf(c))) || (c.ok != null && checksOf(c).length >= 2))) { mk('status', c, 'a verdict and its checks'); const ck = checksOf(c); if (ck.length > 6) mk('table', ck, 'every check', { title: title + ' · checks' }); return done(); }
    // 13 table
    if (rw && rw.length && isObj(rw[0])) { const cols = Object.keys(rw[0]).filter((k) => isScalar(rw[0][k])); if (cols.length >= 2 || rw.length > 1) { const r = mk('table', c, 'rows of the same shape', { map: { rows: rk } }); rowsCompanion(c, r); return done(); } }
    // 14 list
    if (Array.isArray(c) && c.length && c.every((x) => isScalar(x))) { mk('list', c.map((x) => ({ name: String(x) })), 'a list of plain things'); return done(); }
    if (isObj(c)) { const ks = Object.keys(c);
      // 15 numbers
      const nums = ks.filter((k) => typeof c[k] === 'number'); if (nums.length >= 2 && nums.length === ks.length) { mk(nums.length <= 6 ? 'numbers' : 'ranked', c, 'named numbers'); return done(); }
      // 16 prose
      const pk = ['report', 'markdown', 'md', 'summary', 'text', 'answer', 'content'].find((k) => typeof c[k] === 'string' && c[k].trim()); if (pk && (c[pk].length >= 200 || /^#|\n[-*] |\n\n/.test(c[pk]))) { mk('markdown', c[pk], 'prose: ' + pk); if (ks.length > 1) mk('kv', c, 'the rest of the answer', { map: { keys: ks.filter((k) => k !== pk && isScalar(c[k])).slice(0, 12) }, title: title + ' · fields' }); return done(); }
      // 17 record
      if (ks.length >= 1 && ks.length <= 24 && ks.every((k) => isScalar(c[k]))) { mk('kv', c, 'a record: plain fields'); return done(); }
      // 18 json
      mk('json', c, 'structure: a tree to open'); if (rw && rw.length) mk('table', c, 'its rows', { map: { rows: rk }, title: title + ' · ' + rk }); return done(); }
    if (Array.isArray(c) && c.length) { mk('json', c, 'a list of mixed things'); return done(); }
    // 16 · 19 text
    if (typeof c === 'string') { if (isDiffText(c)) mk('diff', c, 'a patch'); else if (c.length >= 200 || /\n/.test(c)) mk('markdown', c, 'prose'); else mk('string', c, 'a short text'); return done(); }
    if (typeof c === 'number') { mk('hero', { value: c }, 'a number'); return done(); }
    if (typeof c === 'boolean') { mk('status', { status: c ? 'ok' : 'failed' }, 'yes or no'); return done(); }
    return done();
    // a companion for rows that carry a name and a number: the same rows ranked by that number
    function rowsCompanion(c0, r) { const R0 = rowsOfAny(c0); if (!R0 || !R0.length || R0.length > 60 || !isObj(R0[0])) return; const nm = ['name', 'title', 'label', 'id', 'key'].find((k) => R0[0][k] != null); const nk0 = numKeys(R0[0]).filter((k) => !/_at$|ts$/.test(k)); if (!nm || !nk0.length || r.form === 'ranked') return;
      mk('ranked', c0, 'the rows ranked by ' + nk0[0], { map: { values: rowsKey(c0), name: nm, value: nk0[0] }, title: title + ' · by ' + nk0[0].replace(/_/g, ' ') }); }
  }
  /* ── A STREAM OF OPERATION → a widget that grows as it arrives ──────────────────────────────────────────────────
     const sink = VeraWidget.fromCapStream(capName, opts?)
       sink.push(event)      one event of the stream: a step {step|stage|name, status|state, ...} · a line {text|msg|
                             message|line, ts?, level?} · a sample {t|ts, v|value|<numbers>} · a token/chunk {delta|token|
                             chunk: "..."} · a string. The first events decide what the stream IS (steps → progress,
                             chunks → markdown, samples → trace, anything else → log, newest in view)
       sink.end(result?)     the stream finished; its final answer (if any) is drawn through fromCapResult beside it
       sink.record()         the live record now: { form, title, source, data, why, stream: true }
       sink.records()        the live record + the final answer's records
       sink.subscribe(fn)    fn(record, sink) after every change (a host redraws; returns an unsubscribe)
       sink.attach(el)       a <vera-widget> that follows the stream (its record set at most once a frame)
     opts: { title, max (events kept, default 500), args } */
  function fromCapStream(capName, opts) {
    opts = opts || {}; const cap = String(capName || ''), max = Math.max(20, +opts.max || 500);
    const S = { kind: '', steps: [], stepIx: {}, lines: [], samples: [], text: '', final: [], ended: false, subs: [], els: [] };
    const kindOf = (e) => { if (typeof e === 'string') return 'lines'; if (!isObj(e)) return 'lines'; if ((e.step != null || e.stage != null || e.phase != null) && (e.status != null || e.state != null || e.done != null)) return 'steps';
      if (typeof (e.delta ?? e.token ?? e.chunk) === 'string') return 'text'; if ((e.t != null || e.ts != null) && (typeof e.v === 'number' || typeof e.value === 'number' || numKeys(e).length) && !firstKey(e, TEXTK)) return 'samples'; return 'lines'; };
    const record = () => { const base = { id: (cap || 'stream').replace(/[^a-z0-9]+/gi, '-') + '-stream', title: opts.title || cap || 'stream', source: cap, read: { args: opts.args || {}, map: {} }, frame: { size: 'l' }, stream: true, ended: S.ended };
      if (S.kind === 'steps') return Object.assign(base, { form: 'progress', data: { steps: S.steps.slice() }, why: 'a stream of steps' });
      if (S.kind === 'text') return Object.assign(base, { form: 'markdown', data: S.text, why: 'a stream of text' });
      if (S.kind === 'samples') { const nk = S.samples.length ? numKeys(S.samples[0]) : []; return nk.length > 1 && !('v' in (S.samples[0] || {})) ? Object.assign(base, { form: 'area', data: S.samples.slice(), read: Object.assign(base.read, { map: { series: '$', split: nk.slice(0, 4) } }), why: 'a stream of samples' }) : Object.assign(base, { form: 'trace', data: S.samples.map((s) => ({ t: s.t ?? s.ts, v: num(s.v ?? s.value ?? s[nk[0]]) })), why: 'a stream of samples' }); }
      return Object.assign(base, { form: 'log', data: S.lines.slice(), why: 'a stream of lines (newest in view)' }); };
    let raf = 0; const notify = () => { const r = record(); S.subs.forEach((f) => { try { f(r, sink); } catch (_) {} });
      if (S.els.length && !raf) { const go = () => { raf = 0; const rr = record(); S.els.forEach((el) => { try { el.setAttribute('record', JSON.stringify(rr)); } catch (_) {} }); }; raf = (typeof requestAnimationFrame === 'function') ? requestAnimationFrame(go) : setTimeout(go, 16); } };
    const sink = {
      push(e) { if (S.ended) return sink; const k = kindOf(e); if (!S.kind) S.kind = k;
        if (k === 'steps') { const id = String(e.step ?? e.stage ?? e.phase ?? e.name); const row = Object.assign({ name: id }, e); if (S.stepIx[id] != null) S.steps[S.stepIx[id]] = Object.assign(S.steps[S.stepIx[id]], row); else { S.stepIx[id] = S.steps.length; S.steps.push(row); } if (S.kind !== 'steps') S.lines.push({ ts: e.ts || new Date().toISOString(), kind: 'step', text: id + ' · ' + String(e.status ?? e.state ?? '') }); }
        else if (k === 'text') { S.text += String(e.delta ?? e.token ?? e.chunk); if (S.kind !== 'text') S.lines.push({ ts: new Date().toISOString(), kind: 'text', text: String(e.delta ?? e.token ?? e.chunk) }); }
        else if (k === 'samples') { S.samples.push(e); if (S.samples.length > max) S.samples.shift(); }
        else { const l = typeof e === 'string' ? { ts: new Date().toISOString(), text: e } : Object.assign({ ts: e.ts || e.t || new Date().toISOString() }, e, { text: e.text ?? e.msg ?? e.message ?? e.line ?? e.event ?? JSON.stringify(e).slice(0, 300) }); S.lines.push(l); if (S.lines.length > max) S.lines.shift(); if (S.kind === 'steps' || S.kind === 'samples') { /* a line inside a step or sample stream rides as the step's detail */ } }
        notify(); return sink; },
      end(result) { S.ended = true; if (result !== undefined) S.final = fromCapResult(cap, result, { args: opts.args, title: (opts.title || cap || 'result') + ' · result' }); notify(); return sink; },
      record, records() { return [record()].concat(S.final); },
      subscribe(fn) { S.subs.push(fn); return () => { S.subs = S.subs.filter((f) => f !== fn); }; },
      attach(el) { if (el && S.els.indexOf(el) < 0) { S.els.push(el); try { el.setAttribute('record', JSON.stringify(record())); } catch (_) {} } return sink; },
      get kind() { return S.kind; }, get ended() { return S.ended; } };
    return sink;
  }

  /* ══ ITEMS, THE CALENDAR FORMS, THE VERA GRAPH FORM (the widget review, round 3) ═════════════════════════════════
     "click an item in a dashboard to see full data in a right hand drawer ... down to the block in iso widgets and per
     widget or section of a widget for standard widgets" · "any lhm items that can be made into widgets ... like the
     calendar controls and even the calendar ... and the schedule view" · "widgetise [the graphs] so they are even more
     re-usable" (the owner).
     ITEMS. Every part a reader can point at carries the thing it draws, whole: data-item holds the row (or the part's
     own object: a donut slice {name, value, share}, a bar {name, value}, a day {date, events}) as JSON, capped at
     ITEM_MAX characters (a bigger row keeps its plain fields and says _trimmed). The element hands it on as a
     'widget:item' event - {record, item, path, ref, data} - and, on a host that asks for it (item-drawer), opens the
     DATA DRAWER on the right of the page that holds the widget. */
  const ITEM_MAX = 6000;
  const itemAttr = (o, path) => { if (o === undefined) return ''; let s = ''; try { s = JSON.stringify(o); } catch (_) { return ''; }
    if (s && s.length > ITEM_MAX && o && typeof o === 'object' && !Array.isArray(o)) { const sh = {}; Object.keys(o).forEach((k) => { const v = o[k]; if (v == null || typeof v !== 'object') sh[k] = typeof v === 'string' ? v.slice(0, 400) : v; }); sh._trimmed = true; s = JSON.stringify(sh); }
    if (!s || s.length > ITEM_MAX * 2) return ''; return ' data-item="' + esc(s) + '"' + (path ? ' data-path="' + esc(String(path)) + '"' : ''); };

  /* ── the calendar: a month, the schedule beside it, and the controls that drive both ──
     month     the month as a grid of days (Monday first): each day's events as chips in their calendar's colour, today
               ringed, the chosen day lit; ‹ › and Today in its head (draw.controls false hides them); a day and an event
               are items (click → the drawer: the day with all its events, or the event whole)
     schedule  what is coming, grouped by day (Today · Tomorrow · Mon 29 Sep): time, title, where, the calendar's colour
     calnav    the controls alone - ‹ Today › and the view - driving every month and schedule of its group (draw.group,
               'cal' by default) on the same page: they move together
     They read cal.events.list (the calendar panel's own read) with arguments that follow the month shown:
     '@month_start' · '@month_end' (the grid's first and last day), '@today', '@today+14d' - resolved at every read. */
  const pad2 = (n) => (n < 10 ? '0' : '') + n;
  const ymdOf = (d) => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  const monthAt = (off) => { const n = new Date(); return new Date(n.getFullYear(), n.getMonth() + (+off || 0), 1); };
  const gridFrom = (m) => { const d = new Date(m); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const WDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  // an argument that follows the calendar: '@today', '@today+14d', '@today-7d', '@month', '@month_start', '@month_end'
  function resolveArgs(args, u) {
    if (!args || typeof args !== 'object') return {}; const off = +((u && u.off) || 0) || 0, out = {};
    Object.keys(args).forEach((k) => { const v = args[k]; if (typeof v !== 'string' || v[0] !== '@') { out[k] = v; return; }
      const m = v.match(/^@today([+-]\d+)d$/); if (m) { out[k] = ymdOf(addDays(new Date(), +m[1])); return; }
      const g = gridFrom(monthAt(off));
      out[k] = v === '@today' ? ymdOf(new Date()) : v === '@month' ? ymdOf(monthAt(off)) : v === '@month_start' ? ymdOf(g) : v === '@month_end' ? ymdOf(addDays(g, 42)) : v; });
    return out; }
  const hasArgTokens = (rec) => !!(rec && rec.read && rec.read.args && Object.values(rec.read.args).some((v) => typeof v === 'string' && v[0] === '@'));
  const calEvents = (d) => { const L = Array.isArray(d) ? d : (d && typeof d === 'object' ? (d.events || d.items || d.rows || []) : []); return L.filter((e) => e && typeof e === 'object' && (e.start != null || e.when != null || e.date != null)); };
  const evWhen = (e) => String(e.start ?? e.when ?? e.date ?? '');
  const dayOfEv = (s) => { const m = String(s).match(/^(\d{4}-\d{2}-\d{2})/); if (m) return m[1]; const d = new Date(s); return isFinite(d) ? ymdOf(d) : ''; };
  const hmOf = (s) => { const m = String(s).match(/T(\d{2}):(\d{2})/); return m ? m[1] + ':' + m[2] : ''; };
  const evCol = (e, i) => e.color || e.colour || e.col || DV(i || 0);
  // every day an event covers (an all-day event's end is the day after it), at most 31
  const evDays = (e) => { const a = dayOfEv(evWhen(e)); if (!a) return []; const endS = String(e.end ?? ''); let b = endS ? dayOfEv(endS) : a; if (!b) b = a;
    if (e.all_day && b > a) b = ymdOf(addDays(new Date(b + 'T00:00:00'), -1)); const out = [a]; let d = new Date(a + 'T00:00:00'); for (let i = 0; i < 31 && ymdOf(d) < b; i++) { d = addDays(d, 1); out.push(ymdOf(d)); } return out; };
  const byDay = (evs) => { const m = {}; evs.forEach((e) => evDays(e).forEach((k) => { (m[k] = m[k] || []).push(e); })); Object.keys(m).forEach((k) => m[k].sort((x, y) => (x.all_day ? -1 : 0) - (y.all_day ? -1 : 0) || evWhen(x).localeCompare(evWhen(y)))); return m; };
  const calHead = (off, o, cls) => { const m = monthAt(off); return '<div class="vb-calh' + (cls ? ' ' + cls : '') + '"><button' + set('off', off - 1) + ' data-calnav="' + (off - 1) + '" title="the month before">‹</button><b>' + MONTHS[m.getMonth()] + ' ' + m.getFullYear() + '</b><button' + set('off', off + 1) + ' data-calnav="' + (off + 1) + '" title="the month after">›</button>' + (off ? '<button class="today"' + set('off', 0) + ' data-calnav="0">Today</button>' : '') + '</div>'; };
  R.month = (d, H, o) => {
    const off = +ui(o, 'off', 0) || 0, sel = String(ui(o, 'sel', '')), today = ymdOf(new Date()); const m = monthAt(off), g = gridFrom(m);
    const evs = calEvents(d), map = byDay(evs); const ctl = !(o && o.draw && o.draw.controls === false);
    const weeks = ymdOf(addDays(g, 35)).slice(0, 7) === ymdOf(m).slice(0, 7) ? 6 : 5, hd = ctl ? 26 : 0, cellH = Math.max(18, Math.floor(((H || 200) - hd - 16) / weeks) - 2), chips = Math.max(0, Math.floor((cellH - 15) / 13));
    let cells = '';
    for (let i = 0; i < weeks * 7; i++) { const day = addDays(g, i), k = ymdOf(day), list = map[k] || [], out = day.getMonth() !== m.getMonth();
      cells += '<div class="vb-mday' + (out ? ' out' : '') + (k === today ? ' today' : '') + (k === sel ? ' sel' : '') + (list.length ? ' has' : '') + '" style="height:' + cellH + 'px"' + itemAttr({ date: k, weekday: WDAYS[(day.getDay() + 6) % 7], count: list.length, events: list.slice(0, 40) }, 'day ' + k) + ' data-vb-sel="sel:' + k + '" data-tip="' + esc(k + (list.length ? '\n' + list.slice(0, 8).map((e) => (hmOf(evWhen(e)) || 'all day') + ' ' + String(e.title ?? e.name ?? '')).join('\n') + (list.length > 8 ? '\n+ ' + (list.length - 8) + ' more' : '') : '\nnothing on')) + '">'
        + '<span class="n">' + day.getDate() + '</span>' + (chips ? list.slice(0, chips).map((e, j) => '<i class="ev"' + itemAttr(e, 'event') + ' style="--c:' + evCol(e, j) + '">' + esc(String(e.title ?? e.name ?? '')) + '</i>').join('') + (list.length > chips ? '<i class="more">+ ' + (list.length - chips) + '</i>' : '') : (list.length ? '<i class="dot" style="background:' + evCol(list[0], 0) + '"></i>' : '')) + '</div>'; }
    return wrap('month', (ctl ? calHead(off, o) : '') + '<div class="vb-mgrid">' + WDAYS.map((w) => '<span class="wd">' + w + '</span>').join('') + cells + '</div>');
  };
  const dayLabel = (k) => { const t = ymdOf(new Date()), tm = ymdOf(addDays(new Date(), 1)); if (k === t) return 'Today'; if (k === tm) return 'Tomorrow'; const d = new Date(k + 'T00:00:00'); return isFinite(d) ? WDAYS[(d.getDay() + 6) % 7] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()].slice(0, 3) : k; };
  R.schedule = (d, H, o) => {
    const evs = calEvents(d); if (!evs.length) return EMPTY('a schedule needs events');
    const sel = String(ui(o, 'sel', '')), from = sel || ymdOf(new Date()); const map = byDay(evs); const days = Object.keys(map).filter((k) => k >= from).sort();
    if (!days.length) return wrap('schedule', '<span class="vb-lbl">nothing on from ' + esc(dayLabel(from)) + '</span>');
    let room = Math.max(3, Math.floor(((H || 200) - 4) / 22)); const parts = [];
    for (const k of days) { if (room < 2) break; parts.push('<div class="vb-sdh' + (k === ymdOf(new Date()) ? ' today' : '') + '"' + itemAttr({ date: k, events: map[k].slice(0, 40) }, 'day ' + k) + '>' + esc(dayLabel(k)) + '<small>' + map[k].length + '</small></div>'); room--;
      for (const e of map[k]) { if (room < 1) break; const a = hmOf(evWhen(e)), b = hmOf(String(e.end ?? '')); parts.push('<div class="vb-sde"' + itemAttr(e, 'event') + ' style="--c:' + evCol(e, 0) + '"><span class="t">' + (e.all_day || !a ? 'all day' : esc(a) + (b ? '<small>' + esc(b) + '</small>' : '')) + '</span><span class="x"><b>' + esc(String(e.title ?? e.name ?? '')) + '</b>' + (e.location ? '<small>' + esc(String(e.location).split(',')[0]) + '</small>' : '') + '</span></div>'); room--; } }
    return wrap('schedule', parts.join(''));
  };
  R.calnav = (d, H, o) => { const off = +ui(o, 'off', 0) || 0, view = String(ui(o, 'view', (o && o.draw && o.draw.view) || 'month'));
    const views = (o && o.draw && Array.isArray(o.draw.views)) ? o.draw.views : ['month', 'schedule'];
    return wrap('calnav', calHead(off, o, 'big') + (views.length > 1 ? '<div class="vb-calv">' + views.map((v) => '<button class="' + (v === view ? 'on' : '') + '"' + set('view', v) + ' data-calview="' + esc(v) + '">' + esc(v) + '</button>').join('') + '</div>' : '') + '<span class="vb-lbl">drives the calendars of group ' + esc(String((o && o.draw && o.draw.group) || 'cal')) + '</span>'); };

  /* ── the Vera graph as a widget form (vgraph) ──
     The estate's own graph (window.veraUI.Graph, /ui/vera-graph.js, loaded the first time a vgraph draws) inside the
     tile: from the record's source (any answer with nodes and edges - a fabric snapshot, a memory graph read, a
     topology) or, with no source, from a layer the graph fetches itself (draw.layer: fabric · entity · memory), shown
     in a display MODE (draw.mode: graph · exploded · estate-3d · estate-2d · mermaid - whatever veraUI.Graph.listModes()
     has registered). The graph lives in the element's light DOM (a slot), so the page's graph styles reach it, and it
     is kept across refreshes (a new answer is loaded into it only when its nodes or edges changed). Without the element
     (VeraWidget.draw alone) the slot shows the flat node graph as its fallback. */
  const toVeraGraph = (d) => { const src = d && typeof d === 'object' ? d : {}; const N = Array.isArray(src) ? src : (src.nodes || src.vertices || []);
    const E = src.edges || src.links || src.rels || src.relationships || [];
    // a node's words: a Neo4j-shaped node (labels[]) keeps its TYPE in label and its words in name/text; a topology node's
    // label is its words and kind its type
    const nodes = (Array.isArray(N) ? N : []).filter((n) => n && typeof n === 'object').map((n) => { const id = String(n.id ?? n.name ?? n.key ?? ''); const typed = Array.isArray(n.labels);
      const words = n.name ?? n.title ?? (typed ? null : n.label) ?? n.text ?? n.summary ?? n.label ?? id;
      return { id, label: String(words ?? id).slice(0, 60), type: String(n.type ?? n.kind ?? (typed ? n.labels[0] : '') ?? n.record_type ?? n.family ?? 'Node') || 'Node', props: n }; }).filter((n) => n.id);
    const edges = (Array.isArray(E) ? E : []).filter((e) => e && typeof e === 'object').map((e) => ({ from: String(e.from ?? e.from_id ?? e.source ?? e.a ?? e.start ?? ''), to: String(e.to ?? e.to_id ?? e.target ?? e.b ?? e.end ?? ''), rel: String(e.rel ?? e.relation ?? e.type ?? e.kind ?? e.label ?? '') })).filter((e) => e.from && e.to);
    return { nodes, edges }; };
  R.vgraph = (d, H, o) => { const G = toVeraGraph(d); const layer = o && o.draw && o.draw.layer; if (!G.nodes.length && !layer) return EMPTY('a graph needs nodes (and edges), or draw.layer');
    const mode = String((o && o.draw && o.draw.mode) || 'graph'), h = Math.max(90, (H || 200) - 14);
    let fb = ''; try { fb = G.nodes.length ? R.graph({ nodes: G.nodes.map((n) => ({ id: n.id, label: n.label, family: n.type })), links: G.edges.map((e) => ({ source: e.from, target: e.to, from: e.from, to: e.to })) }, h, o) : ''; } catch (_) { fb = ''; }
    return wrap('vgraph', '<div class="vb-vgraph" style="height:' + h + 'px"><slot name="vgraph">' + fb + '</slot></div><span class="vb-lbl">' + (G.nodes.length ? G.nodes.length + ' nodes · ' + G.edges.length + ' edges' : 'the ' + esc(layer) + ' graph') + '</span>'); };
  let _vgLoad = null;
  const ensureVeraGraph = (base) => { if (window.veraUI && window.veraUI.Graph && window.veraUI.Graph.create) return Promise.resolve(window.veraUI.Graph);
    if (_vgLoad) return _vgLoad; _vgLoad = new Promise((ok) => { const s = document.createElement('script'); s.src = (base || '') + '/ui/vera-graph.js'; s.onload = () => ok(window.veraUI && window.veraUI.Graph); s.onerror = () => { _vgLoad = null; ok(null); }; document.head.appendChild(s); }); return _vgLoad; };

  /* colour for a Vera graph in a tile (draw.colour: 'status'): each type (the estate's planes: clients, work, core, services,
     runtimes, hosts, devices) its own colour, a node whose status is a problem in red or amber - the key says which is which */
  const VG_PAL = ['#5b9bd5', '#6dbf7b', '#c678dd', '#e0a23c', '#56b6c2', '#d19a66', '#98c379', '#e5c07b', '#61afef', '#be5046'];
  const VG_BAD = '#e5534b', VG_WARN = '#e0a23c';
  const vgStatus = (p) => { const s = String((p && (p.status ?? p.state ?? p.health)) ?? '').toLowerCase(); return /^(down|fail|failed|error|err|dead|offline|stopped|unreachable|timeout)$/.test(s) ? 'bad' : /^(warn|warning|degraded|stale|busy|paused|pending|queued)$/.test(s) ? 'warn' : ''; };
  function vgColour(G) { const types = []; G.nodes.forEach((n) => { if (!types.includes(n.type)) types.push(n.type); });
    const tcol = {}; types.forEach((t, i) => { tcol[t] = VG_PAL[i % VG_PAL.length]; });
    let bad = 0, warn = 0; G.nodes.forEach((n) => { const st = vgStatus(n.props); n.color = st === 'bad' ? VG_BAD : st === 'warn' ? VG_WARN : tcol[n.type]; if (st === 'bad') bad++; if (st === 'warn') warn++; });
    return { tcol, bad, warn }; }
  function vgKey(host, k) { let el = host.querySelector(':scope > .vw-vgkey'); if (!el) { el = document.createElement('div'); el.className = 'vw-vgkey'; host.appendChild(el); }
    el.innerHTML = Object.keys(k.tcol).map((t) => '<span><i style="background:' + k.tcol[t] + '"></i>' + esc(t) + '</span>').join('') + (k.warn ? '<span><i style="background:' + VG_WARN + '"></i>warning ' + k.warn + '</span>' : '') + (k.bad ? '<span><i style="background:' + VG_BAD + '"></i>problem ' + k.bad + '</span>' : ''); }
  function mountVeraGraph(el, rec, data, size) {
    const draw0 = (rec && rec.draw) || {}, mode = String(draw0.mode || 'graph'), layer = draw0.layer ? String(draw0.layer) : '';
    const minC = draw0.chrome === 'min'; let host = el._vgHost; if (!host) { host = document.createElement('div'); host.setAttribute('slot', 'vgraph'); host.className = 'vw-vgraph-host' + (minC ? ' min' : ''); host.style.cssText = 'width:100%;height:100%;min-height:80px;position:relative;display:flex;flex-direction:column'; el.appendChild(host); el._vgHost = host; }
    return ensureVeraGraph(el.base).then((Gr) => { if (!Gr || el._rec !== rec) return;
      let g = el._vg; const big = size === 'l' || size === 'xl';
      if (!g || el._vgBig !== big) { if (g && g.destroy) { try { g.destroy(); } catch (_) {} } host.innerHTML = ''; g = el._vg = Gr.create(host, { height: 'fill', showSearch: big, showLegend: size === 'xl' && !minC, showLeftPanel: size === 'xl' && !minC, sidebar: false, actionsEnabled: false, subscribeLiveEvents: false, apiBase: el.base || '',
          // a node is an item like any other: on a host with the drawer, a click opens the drawer on the node's own data
          onNodeClick: (node) => { if (!el.hasAttribute('item-drawer')) return; const it = (node && node.props && typeof node.props === 'object') ? node.props : node; const rec2 = recOf(el), detail = { record: rec2, item: it, path: 'node ' + (node && node.id), ref: rowRef(it), data: el._data, host: el };
            let go = true; try { go = el.dispatchEvent(new CustomEvent('widget:item', { bubbles: true, composed: true, cancelable: true, detail })); } catch (_) {} if (go) drawer(detail); return false; } }); el._vgBig = big; el._vgSig = ''; el._vgMode = ''; el._vgMode0 = undefined; el._vgSet = false; }
      if (layer && !rec.source) { const sig = 'layer:' + layer + ':' + JSON.stringify(resolveArgs(rec.read && rec.read.args, el._ui)); if (sig !== el._vgSig) { el._vgSig = sig; try { g.fetchSnapshot(layer, resolveArgs(rec.read && rec.read.args, el._ui)); } catch (_) {} } }
      else { const G = toVeraGraph(data); const ck = draw0.colour === 'status' ? vgColour(G) : null; if (ck) vgKey(host, ck); const sig = G.nodes.length + ':' + G.edges.length + ':' + G.nodes.slice(0, 50).map((n) => n.id + (n.color || '')).join(','); if (sig !== el._vgSig) { el._vgSig = sig; try { g.load(G); } catch (_) {} } }
      /* the mode is the record's the first time (or the one the viewer chose before, kept per widget); after that it is left
         alone - re-applying it on every refresh flicked Live operations back to Estate 3D from Exploded (2026-09-28). A change
         counts as the viewer's only once the mode was really applied: the modes load after the graph (vera-graph-modes.js), so
         the first setMode can find none and leave 'graph' - that is retried, never stored as a pick */
      { const mk = 'vera.vgraph.pick.' + key(rec), cur = g.getMode ? g.getMode() : '';
        const apply = () => { try { if (g.setMode && (g.getMode ? g.getMode() : '') !== el._vgMode) g.setMode(el._vgMode); } catch (_) {} el._vgSet = !g.getMode || g.getMode() === el._vgMode; return el._vgSet; };
        if (el._vgSet && cur && cur !== el._vgMode) { el._vgMode = cur; try { localStorage.setItem(mk, cur); } catch (_) {} }
        else if (!el._vgSet || el._vgMode0 !== mode) { let want = (el._vgMode0 === mode && el._vgMode) || mode; if (el._vgMode0 === undefined) { try { want = localStorage.getItem(mk) || mode; } catch (_) {} }
          el._vgMode = want; el._vgMode0 = mode; if (cur !== want) { try { if (g.setMode) g.setMode(want); } catch (_) {} } if (!apply()) { let n = 0; const again = () => { if (el._vg !== g || apply() || ++n > 20) return; setTimeout(again, 400); }; setTimeout(again, 400); } } }
      try { g.resize && g.resize(); } catch (_) {}
      return g; }); }
  function unmountVeraGraph(el) { if (el._vg && el._vg.destroy) { try { el._vg.destroy(); } catch (_) {} } el._vg = null; if (el._vgHost) { el._vgHost.remove(); el._vgHost = null; } el._vgSig = ''; }


  /* ── CI: automated development work, drawn (ci_view_core.py computes the same payload server side) ─────────────────
     Nine forms over ONE payload ({kind:'ci', view, lanes|tests|columns|stages|cards|series, summary}) - the ci.* and
     loop.ci.* capabilities answer in it, and ciOf() adapts the Loop Lab capabilities' own answers into it
     (evolve.unittest.history, evolve.pipeline.list/get, board.items, workshop.agent_loop.trace, evolve.tasks.overview,
     evolve.sandbox.list) so calling any of them in chat lands the Loop Lab picture on the canvas.
       status-matrix  lanes of runs, one cell per run, newest at the right; the older cells a lane had no room for are
                      counted at its left, never dropped silently
       race-green     the same lanes as a race: red laps, the runner, the flag when a lane came good, attempts + time
       test-grid      every test that failed in any run × the runs: broken · flaky · fixed · unknown (never a false pass)
       ci-board       board columns, each card with its agent and its pipeline's gate
       run-track      one pipeline's stages as a track, every step's full text at XL
       run-compare    run A → run B: fixed · broken · still failing, the counts moved
       ci-pulse       pass rate, runs per day (pass/red), the agents behind them, the streak
       ci-fleet       the sandboxes: who is working where, gate state, conversations
       ci-run         one run whole: its cell, its track, its lane, its failing tests, its board items, its conversation
     Every cell and card carries its record (data-item) so a click opens the drawer on it, and data-ci-ref (a run's ts
     or pipeline id, a lane, a test id) so a host page (Loop Lab) can drill to the run itself. */
  const CI_AG = { claude: 'var(--b-dv1)', codex: 'var(--b-dv2)', vera: 'var(--b-dv3)', user: 'var(--b-dv5)', unattributed: 'var(--b-t3)' };
  const ciAg = (a) => CI_AG[String(a || '').toLowerCase()] || (a ? DV(String(a).length + 3) : B.t3);
  const ciAgent = (a) => a ? '<span class="ci-ag" style="--ag:' + ciAg(a) + '">' + esc(a) + '</span>' : '';
  function ciCtl() { for (let i = 0; i < arguments.length; i++) { const k = String(arguments[i] == null ? '' : arguments[i]).trim().toLowerCase(); if (!k) continue; if (/^claude/.test(k)) return 'claude'; if (/^codex|openai/.test(k)) return 'codex'; if (/^(vera|loop|autonomous|census|dream|improve|agent_loop|scheduler|idle)/.test(k)) return 'vera'; if (/^(user|human|ui|browser)$/.test(k)) return 'user'; return k; } return ''; }
  function ciO(r) { const st = String((r && (r.status ?? r.state)) ?? '').toLowerCase();
    if (/^(running|live|queued|pending|drafting|gating|testing)$/.test(st) || (r && r.live === true && r.ok == null)) return 'running';
    if ((r && r.ok === true) || /^(pass|passed|ok|green|done|merged|promoted|success)$/.test(st)) return 'pass';
    if (r && +r.errors && !+r.failed) return 'error';
    if ((r && r.ok === false) || /^(fail|failed|red|error|rolled_back|rejected|timeout|cancelled)$/.test(st)) return 'fail';
    if (/^(warn|partial|degraded)$/.test(st)) return 'warn'; if (/^(skip|skipped)$/.test(st)) return 'skip'; return 'unknown'; }
  const ciRed = (o) => o === 'fail' || o === 'error';
  const ciSecs = (a, b) => { const x = Date.parse(a), y = Date.parse(b); return isFinite(x) && isFinite(y) ? (y - x) / 1000 : null; };
  const ciDur = (s) => { if (s == null || !isFinite(s)) return ''; s = Math.abs(s); return s < 90 ? Math.round(s) + 's' : s < 5400 ? Math.round(s / 60) + 'm' : s < 172800 ? (Math.round(s / 360) / 10) + 'h' : Math.round(s / 86400) + 'd'; };
  const ciWhen = (t) => { const s = String(t || ''); const m = s.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/); return m ? m[3] + '/' + m[2] + ' ' + m[4] + ':' + m[5] : s.slice(0, 16); };
  const ciAgo = (t) => { const x = Date.parse(t); if (!isFinite(x)) return ''; return ciDur((Date.now() - x) / 1000) + ' ago'; };
  function ciCell(r, prev) { const c = { id: String(r.run_id ?? r.id ?? r.pipeline_id ?? r.ts ?? ''), ts: String(r.ts ?? r.created_at ?? ''), o: r.o || ciO(r), passed: +r.passed || 0, failed: +r.failed || 0, errors: +r.errors || 0, total: +r.total || 0,
      pipeline_id: String(r.pipeline_id ?? ''), controller: ciCtl(r.controller, r.via), session_id: String(r.session_id ?? ''), summary: String(r.summary ?? r.title ?? '') };
    if (r.label) c.label = String(r.label); if (r.ms != null) c.ms = +r.ms; if (r.score != null) c.score = +r.score;
    const f = Array.isArray(r.failures) ? r.failures.map((x) => String((x && (x.node_id || x.name)) || '')).filter(Boolean) : (Array.isArray(r.failing) ? r.failing : null); if (f && f.length) c.failing = f;
    if (prev) c.delta = c.total - (+prev.total || 0); return c; }
  function ciRace(cells) { const laps = []; let start = null, reds = 0;
    cells.forEach((c) => { if (ciRed(c.o)) { if (!start) { start = c; reds = 0; } reds++; } else if (c.o === 'pass' && start) { laps.push({ went_red_at: start.ts, went_green_at: c.ts, red_runs: reds, attempts: reds + 1, seconds: ciSecs(start.ts, c.ts), from: start.id, to: c.id }); start = null; reds = 0; } });
    const dec = cells.filter((c) => c.o === 'pass' || ciRed(c.o)); const last = dec[dec.length - 1]; const running = cells.length && cells[cells.length - 1].o === 'running';
    let streak = 0; for (let i = dec.length - 1; i >= 0 && dec[i].o === 'pass'; i--) streak++;
    const state = !last ? (running ? 'racing' : 'none') : last.o === 'pass' ? 'green' : (running ? 'racing' : 'red');
    const out = { state, streak, laps, runs: cells.length }; if (start) { out.red_since = start.ts; out.red_runs = reds; } if (laps.length) { out.last_lap = laps[laps.length - 1]; out.best_attempts = Math.min(...laps.map((l) => l.attempts)); } return out; }
  function ciLanes(rows0, key) { const by = new Map(); rows0.filter(isObj).slice().sort((a, b) => String(a.ts ?? a.created_at ?? '').localeCompare(String(b.ts ?? b.created_at ?? ''))).forEach((r) => { const k = String((key ? key(r) : (r.branch ?? r.label)) || 'unnamed'); if (!by.has(k)) by.set(k, []); by.get(k).push(r); });
    const lanes = []; by.forEach((rs, k) => { const cells = rs.map((r, i) => ciCell(r, rs[i - 1])); const race = ciRace(cells); const last = cells[cells.length - 1] || {};
      lanes.push({ id: k, name: k, cells, hidden: 0, race, state: race.state, controllers: [...new Set(cells.map((c) => c.controller).filter(Boolean))], last_ts: last.ts || '', total: last.total || 0, passed: last.passed || 0, failed: last.failed || 0, pipeline_id: (cells.slice().reverse().find((c) => c.pipeline_id) || {}).pipeline_id || '' }); });
    return lanes.sort((a, b) => String(b.last_ts).localeCompare(String(a.last_ts))); }
  function ciSummary(lanes) { const cells = lanes.flatMap((l) => l.cells || []), dec = cells.filter((c) => c.o === 'pass' || ciRed(c.o)), green = dec.filter((c) => c.o === 'pass').length; const states = {}, ctl = {};
    lanes.forEach((l) => { states[l.state] = (states[l.state] || 0) + 1; }); cells.forEach((c) => { const k = c.controller || 'unattributed'; const e = ctl[k] = ctl[k] || { runs: 0, pass: 0, red: 0 }; e.runs++; if (c.o === 'pass') e.pass++; else if (ciRed(c.o)) e.red++; });
    const laps = lanes.flatMap((l) => (l.race && l.race.laps) || []); const med = (xs) => { xs = xs.filter((x) => x != null).sort((a, b) => a - b); if (!xs.length) return null; const m = xs.length >> 1; return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; };
    return { lanes: lanes.length, runs: cells.length + lanes.reduce((s, l) => s + (l.hidden || 0), 0), shown: cells.length, green, red: dec.length - green, running: cells.filter((c) => c.o === 'running').length, pass_rate: dec.length ? green / dec.length : null, states, controllers: ctl, laps: laps.length, median_attempts: med(laps.map((l) => l.attempts)), median_seconds_to_green: med(laps.map((l) => l.seconds)) }; }
  const CI_TRACK = ['begin', 'commit', 'compile', 'tests', 'review', 'promote'];
  const CI_STAGE = { begin: 'begin', adopt: 'begin', draft: 'begin', branch: 'begin', commit: 'commit', commits: 'commit', edit: 'commit', apply: 'commit', compile: 'compile', gate: 'compile', syntax: 'compile', 'critical-tests': 'tests', tests: 'tests', unittest: 'tests', test: 'tests', review: 'review', review_request: 'review', critic: 'review', promote: 'promote', merge: 'promote', rollback: 'promote' };
  function ciTrack(p) { const S = {}; CI_TRACK.forEach((s) => { S[s] = { name: s, status: 'todo', steps: [] }; });
    (p.steps || []).filter(isObj).forEach((st) => { const n = String(st.stage || '').toLowerCase(); const k = CI_STAGE[n] || (Object.keys(CI_STAGE).find((x) => n.includes(x)) ? CI_STAGE[Object.keys(CI_STAGE).find((x) => n.includes(x))] : 'commit'); S[k].steps.push({ stage: st.stage, ok: st.ok, detail: String(st.detail ?? ''), ts: String(st.ts ?? '') }); S[k].status = st.ok ? 'done' : 'failed'; S[k].ts = st.ts; });
    const dec = String(p.decision || '').toLowerCase(); if (/promoted|merged/.test(dec)) S.promote.status = 'done'; else if (/rolled_back|rejected/.test(dec)) S.promote.status = 'failed';
    if (p.review_requested && S.review.status === 'todo') S.review.status = 'now'; if (p.gate_passed === true && S.tests.status === 'todo') S.tests.status = 'done'; if (p.gate_passed === false && S.tests.status === 'todo') S.tests.status = 'failed';
    const ord = CI_TRACK.map((s) => S[s]); if (/running|gating|testing|drafting/.test(String(p.status || '')) || p.live) { const nx = ord.find((s) => s.status === 'todo'); if (nx) nx.status = 'now'; }
    return { kind: 'ci', view: 'track', title: p.branch || p.id, pipeline: p, controller: ciCtl(p.controller, p.via), stages: ord, commits: p.commits || [], changed_files: p.changed_files || [] }; }
  // any answer → the ci payload the form draws (null when it is not one)
  function ciOf(d, form) {
    if (!d || typeof d !== 'object') return null; if (d.type === 'tool_result' && d.content) d = d.content;
    if (d.kind === 'ci') return d;
    const mx = (lanes, extra) => Object.assign({ kind: 'ci', view: form === 'race-green' ? 'race' : 'matrix', lanes, summary: ciSummary(lanes) }, extra || {});
    if (Array.isArray(d.runs) && d.runs.length && isObj(d.runs[0]) && ('passed' in d.runs[0] || 'total' in d.runs[0])) return mx(ciLanes(d.runs), { title: 'Gate runs', source: 'evolve.unittest.history' });
    if (Array.isArray(d.pipelines)) return mx(ciLanes(d.pipelines.map((p) => Object.assign({}, p, { ts: p.created_at, pipeline_id: p.id, ok: p.gate_passed === true ? true : p.gate_passed === false ? false : (/promoted|merged/.test(String(p.decision || '')) ? true : /rolled_back|rejected/.test(String(p.decision || '')) ? false : null), status: p.live ? 'running' : '', summary: [p.status, p.decision].filter(Boolean).join(' · ') }))), { title: 'Pipelines', source: 'evolve.pipeline.list' });
    if (isObj(d.pipeline) && Array.isArray(d.pipeline.steps)) return ciTrack(d.pipeline);
    if (Array.isArray(d.steps) && isObj(d.plan)) { const plan = (d.plan.steps || []).filter(isObj), ex = {}; (d.steps || []).filter(isObj).forEach((s) => { ex[String(s.step_id)] = s; }); const ids = plan.map((p) => String(p.id)).concat(Object.keys(ex).filter((k) => !plan.some((p) => String(p.id) === k)));
      const steps = ids.map((id) => { const p = plan.find((x) => String(x.id) === id) || {}, e = ex[id] || {}; return { id, title: String(e.title || p.title || 'step ' + id), calls: (e.calls || []).filter(isObj), ok: e.ok, executed: !!ex[id] }; });
      if (form === 'ci-board') { const cols = { planned: [], running: [], done: [], failed: [] }; steps.forEach((s) => { const ln = s.ok === true ? 'done' : s.ok === false ? 'failed' : s.executed ? 'running' : 'planned'; cols[ln].push({ id: s.id, title: s.title, lane: ln, calls: s.calls.length, ms: s.calls.reduce((a, c) => a + (+c.ms || 0), 0) }); }); return { kind: 'ci', view: 'board', title: (d.run && d.run.goal) || 'Loop plan', columns: Object.keys(cols).map((k) => ({ name: k, items: cols[k] })) }; }
      const lanes = steps.map((s) => { const cells = s.calls.map((c) => ({ id: s.id + '.' + (c.cycle || ''), ts: '', o: c.ok ? 'pass' : c.ok === false ? 'fail' : 'unknown', label: String(c.tool || ''), ms: +c.ms || 0, summary: String(c.args || ''), controller: 'vera' })); const race = ciRace(cells); race.state = s.ok ? 'green' : s.ok === false ? 'red' : s.executed ? 'racing' : 'none'; return { id: s.id, name: s.title, cells, hidden: 0, race, state: race.state, controllers: ['vera'], total: cells.length }; });
      if (Array.isArray(d.gates) && d.gates.length) { const cells = d.gates.filter(isObj).map((g) => ({ id: 'gate.' + g.round, ts: '', o: g.complete ? 'pass' : 'fail', label: 'round ' + g.round, summary: (g.missing || []).join('; ') || 'complete' })); const race = ciRace(cells); lanes.unshift({ id: 'gate', name: 'completion gate', cells, hidden: 0, race, state: race.state, controllers: ['vera'], total: cells.length }); }
      return mx(lanes, { title: (d.run && d.run.goal) || 'Loop', session_id: d.session_id, source: 'workshop.agent_loop.trace' }); }
    if (Array.isArray(d.tasks) && d.tasks.length && isObj(d.tasks[0]) && ('series' in d.tasks[0] || 'stats' in d.tasks[0])) { const lanes = d.tasks.filter(isObj).map((t) => { const s = Array.isArray(t.series) ? t.series : []; const cells = s.map((v, i) => { if (isObj(v) && (v.ok != null || v.status)) return { id: String(v.run_id || (t.task_id || '') + ':' + i), ts: String(v.ts || ''), o: ciO(v), label: String(v.driver || ''), summary: [v.driver ? 'driver ' + v.driver : '', v.status, v.wall_s != null ? Math.round(v.wall_s) + 's' : '', v.code ? '@' + v.code : ''].filter(Boolean).join(' · '), controller: /^(claude|codex|vera|user)/i.test(String(v.driver || '')) ? ciCtl(v.driver) : '' }; const x = num(isObj(v) ? (v.score ?? v.v ?? v.value) : v); return { id: (t.task_id || '') + ':' + i, ts: '', o: x >= 0.999 ? 'pass' : x <= 0 ? 'fail' : 'warn', score: x, controller: '' }; }); const race = ciRace(cells); return { id: String(t.task_id || t.name || ''), name: String((t.task && (t.task.name || t.task.title)) || t.name || t.task_id || ''), cells, hidden: 0, race, state: race.state, controllers: [...new Set(cells.map((c) => c.controller).filter(Boolean))], last_ts: String((t.last && (t.last.ts || t.last.at)) || '') }; }); return mx(lanes, { title: 'Tasks', source: 'evolve.tasks.overview' }); }
    if (Array.isArray(d.items) && d.items.length && isObj(d.items[0]) && 'lane' in d.items[0]) { const by = {}; d.items.filter(isObj).forEach((it) => { const k = String(it.lane || 'inbox'); (by[k] = by[k] || []).push({ id: it.id, title: it.title, lane: k, agent: ciCtl(it.agent) || it.agent, branch: it.branch || '', pipeline: it.pipeline || '', session: it.session || '', labels: it.labels || [], comments: +it.comment_count || 0, updated_at: it.updated_at || it.created_at || '' }); });
      const order = ['inbox', 'ready', 'queued_vera', 'in_progress', 'in_progress_vera', 'blocked', 'needs_review', 'review', 'done', 'dropped']; const names = order.filter((k) => by[k]).concat(Object.keys(by).filter((k) => !order.includes(k)).sort()); return { kind: 'ci', view: 'board', title: 'Board', columns: names.map((k) => ({ name: k, items: by[k] })) }; }
    if (Array.isArray(d.sandboxes)) { const cards = d.sandboxes.filter(isObj).map((s) => ({ name: s.name, branch: s.branch, role: s.role, running: !!s.running, pinned: !!s.pinned, port: s.port, url: s.url, owner: ciCtl(s.owner) || s.owner || '', last_activity: s.last_activity || '', gate: '', conversations: 0 })); return { kind: 'ci', view: 'fleet', title: 'Fleet', cards, summary: { sandboxes: cards.length, running: cards.filter((c) => c.running).length } }; }
    const rw = rowsOfAny(d); if (rw && rw.length && isObj(rw[0]) && ('ok' in rw[0] || 'status' in rw[0] || 'passed' in rw[0])) return mx(ciLanes(rw));
    return null;
  }
  const CI_SAMPLE = () => { const T = (h, m) => '2026-09-27T' + String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':00Z'; const R0 = [];
    const add = (b, who, pat, h0) => pat.split('').forEach((ch, i) => R0.push({ ts: T(h0 + (i >> 1), (i % 2) * 30), branch: b, controller: who, ok: ch === 'p' ? true : ch === 'f' ? false : null, status: ch === 'r' ? 'running' : '', passed: 5190 + i, failed: ch === 'f' ? 2 : 0, total: 5192 + i, failures: ch === 'f' ? [{ node_id: 'tests/test_ship.py::test_' + b.slice(-4) }] : [] }));
    add('feat/loop-lab-widgets', 'claude', 'ffpppffp', 8); add('fix/gate-budget', 'codex', 'fffp', 9); add('feat/census-yield', 'vera', 'ppfr', 10); add('feat/ops-live-view', 'claude', 'pppp', 7); add('fix/sandbox-menu', 'user', 'ff', 12);
    const lanes = ciLanes(R0); return { kind: 'ci', view: 'matrix', title: 'Gate runs', lanes, summary: ciSummary(lanes) }; };
  Object.assign(DRAWN, { 'status-matrix': 'matrix', 'race-green': 'matrix', 'test-grid': 'matrix', 'ci-board': 'items', 'run-track': 'stages', 'run-compare': 'values', 'ci-pulse': 'values', 'ci-fleet': 'items', 'ci-run': 'values' });
  const CI_GLYPH = { 'status-matrix': '▦', 'race-green': '⚑', 'test-grid': '▤', 'ci-board': '▥', 'run-track': '⋯', 'run-compare': '⇄', 'ci-pulse': '∿', 'ci-fleet': '⊞', 'ci-run': '◉' };
  Object.assign(FORM_SAMPLE, {
    'status-matrix': CI_SAMPLE, 'race-green': () => Object.assign(CI_SAMPLE(), { view: 'race', title: 'Race to green' }),
    'test-grid': () => { const cols = ['1', '2', '3', '4', '5', '6', '7'].map((i) => ({ id: 'r' + i, ts: '2026-09-2' + i + 'T10:00:00Z', o: 'fail', branch: 'feat/x' })); return { kind: 'ci', view: 'tests', title: 'Tests across runs', columns: cols, tests: [
      { id: 'tests/test_ship.py::test_branch_rows', name: 'test_branch_rows', module: 'tests/test_ship.py', cells: ['-', 'fail', 'fail', 'fail', 'fail', 'fail', 'fail'], class: 'broken', fails: 6, flips: 0, description: 'AssertionError: 7 != 8' },
      { id: 'tests/test_gate.py::test_budget', name: 'test_budget', module: 'tests/test_gate.py', cells: ['pass', 'fail', 'pass', 'fail', 'pass', 'pass', 'fail'], class: 'flaky', fails: 3, flips: 5, description: 'timeout after 900s' },
      { id: 'tests/test_census.py::test_yield', name: 'test_yield', module: 'tests/test_census.py', cells: ['fail', 'fail', 'fail', 'pass', 'pass', 'pass', 'pass'], class: 'fixed', fails: 3, flips: 1 },
      { id: 'tests/test_menu.py::test_pinned', name: 'test_pinned', module: 'tests/test_menu.py', cells: ['-', '-', '-', '-', 'fail', 'pass', 'unknown'], class: 'fixed', fails: 1, flips: 1 }], summary: { tests: 4, runs: 7, classes: { broken: 1, flaky: 1, fixed: 2 } } }; },
    'ci-board': () => ({ kind: 'ci', view: 'board', title: 'Board', columns: [
      { name: 'ready', items: [{ id: 'b1', title: 'Race to green by agent', agent: 'codex', branch: 'feat/race-agents' }, { id: 'b2', title: 'Flaky test quarantine', agent: 'vera' }] },
      { name: 'in_progress', items: [{ id: 'b3', title: 'Loop Lab widgets on the canvas', agent: 'claude', branch: 'feat/loop-lab-widgets', gate: 'fail', pipeline: 'e140fee0' }, { id: 'b4', title: 'Census yield, not pause', agent: 'vera', branch: 'feat/census-yield', gate: 'pass' }] },
      { name: 'needs_review', items: [{ id: 'b5', title: 'Gate budget fits the tier', agent: 'codex', branch: 'fix/gate-budget', gate: 'pass' }] },
      { name: 'done', items: [{ id: 'b6', title: 'Sandbox menu shows merges', agent: 'claude', gate: 'pass', decision: 'promoted' }, { id: 'b7', title: 'Mirror refresh', agent: 'user', gate: 'pass' }] }] }),
    'run-track': () => ciTrack({ id: 'e140fee0', branch: 'feat/loop-lab-widgets', controller: 'claude_code', status: 'gating', live: true, gate_passed: null, commits: ['3d5f', 'a07e'], changed_files: ['vera/evolve/ci_view_core.py', 'vera/widgets/widget_element.js'],
      steps: [{ stage: 'begin', ok: true, detail: 'branch + worktree ready (own container)', ts: '2026-09-27T19:30:00Z' }, { stage: 'commit', ok: true, detail: '2 commits', ts: '2026-09-27T20:10:00Z' }, { stage: 'compile', ok: true, detail: '4 files parse', ts: '2026-09-27T20:11:00Z' }] }),
    'run-compare': () => ({ kind: 'ci', view: 'compare', title: 'Compare runs', a: { id: 'a', ts: '2026-09-27T10:00:00Z', branch: 'feat/x', o: 'fail', controller: 'claude', passed: 5188, failed: 3, errors: 0, skipped: 33, total: 5224 }, b: { id: 'b', ts: '2026-09-27T10:40:00Z', branch: 'feat/x', o: 'fail', controller: 'claude', passed: 5195, failed: 1, errors: 0, skipped: 33, total: 5229 },
      delta: { passed: 7, failed: -2, errors: 0, skipped: 0, total: 5 }, seconds: 2400, fixed: [{ id: 'tests/test_ship.py::test_rows' }, { id: 'tests/test_gate.py::test_budget' }, { id: 'tests/test_ci.py::test_race' }], broken: [{ id: 'tests/test_menu.py::test_pinned', description: 'KeyError: merged' }], still: [] }),
    'ci-pulse': () => { const s = Array.from({ length: 14 }, (_, i) => { const runs = 6 + ((i * 7) % 9), red = (i * 5) % 4; return { t: '2026-09-' + String(14 + i).padStart(2, '0'), runs, pass: runs - red, red, rate: (runs - red) / runs, tests: 5100 + i * 9 }; }); return { kind: 'ci', view: 'pulse', title: 'CI pulse', series: s, summary: { runs: 140, green: 112, red: 28, pass_rate: 0.8, lanes: 22, states: { green: 17, red: 3, racing: 2 }, controllers: { claude: { runs: 70, pass: 58, red: 12 }, codex: { runs: 38, pass: 29, red: 9 }, vera: { runs: 24, pass: 19, red: 5 }, user: { runs: 8, pass: 6, red: 2 } }, median_attempts: 2, median_seconds_to_green: 1500 }, latest: { o: 'pass', ts: '2026-09-27T19:20:00Z', passed: 5193, total: 5227 } }; },
    'ci-fleet': () => ({ kind: 'ci', view: 'fleet', title: 'Fleet', cards: [
      { name: 'vera-dev-feat-loop-lab-widgets', branch: 'feat/loop-lab-widgets', running: true, owner: 'claude', gate: 'fail', pipeline: 'e140fee0', conversations: 2, port: 8989 },
      { name: 'vera-dev-bleeding-edge-mirror', branch: 'loop-lab/bleeding-edge-mirror', running: true, pinned: true, owner: 'user', gate: 'pass', port: 8981 },
      { name: 'vera-dev-fix-gate-budget', branch: 'fix/gate-budget', running: true, owner: 'codex', gate: 'pass', conversations: 1, port: 8987 },
      { name: 'vera-dev-feat-census-yield', branch: 'feat/census-yield', running: false, owner: 'vera', gate: '', port: 8990 }], summary: { sandboxes: 4, running: 3, red: 1 } }),
    'ci-run': () => { const sm = CI_SAMPLE(); const ln = sm.lanes[0]; const c = ln.cells[ln.cells.length - 1]; return { kind: 'ci', view: 'run', title: ln.name, cell: Object.assign({}, c, { o: 'fail', failed: 1, failing: ['tests/test_ship.py::test_branch_rows'] }), run: { failures: [{ node_id: 'tests/test_ship.py::test_branch_rows', description: 'AssertionError: 7 != 8 - the merged branch lost its row' }] }, lane: ln, track: FORM_SAMPLE['run-track'](), board_items: [{ id: '602e4089', title: 'Loop Lab on the canvas', lane: 'in_progress' }], conversation: { session_id: '045549bb', controller: 'claude' } }; },
  });

  /* the pieces every CI face shares */
  const ciPct = (x) => x == null ? '—' : Math.round(x * 100) + '%';
  const ciRing = (fr, sz, col) => { sz = sz || 44; const r = sz / 2 - 4, C = 2 * Math.PI * r, f = fr == null ? 0 : Math.max(0, Math.min(1, fr)); return '<svg class="ci-ring" viewBox="0 0 ' + sz + ' ' + sz + '" width="' + sz + '" height="' + sz + '"><circle cx="' + sz / 2 + '" cy="' + sz / 2 + '" r="' + r + '" fill="none" stroke="var(--b-s3)" stroke-width="4"/><circle cx="' + sz / 2 + '" cy="' + sz / 2 + '" r="' + r + '" fill="none" stroke="' + (col || (f >= .9 ? B.ac2 : f >= .6 ? B.ac3 : B.ac4)) + '" stroke-width="4" stroke-linecap="round" stroke-dasharray="' + (C * f).toFixed(1) + ' ' + C.toFixed(1) + '" transform="rotate(-90 ' + sz / 2 + ' ' + sz / 2 + ')"/><text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle">' + ciPct(fr) + '</text></svg>'; };
  const ciState = (s) => '<i class="ci-st st-' + esc(s || 'none') + '" title="' + esc(s || 'none') + '"></i>';
  const ciTip = (c, lane) => [lane ? lane + ' ·' : '', c.label || '', c.o, c.ts ? ciWhen(c.ts) : '', c.total ? (c.passed + '/' + c.total + ' passed' + (c.failed ? ' · ' + c.failed + ' failed' : '') + (c.errors ? ' · ' + c.errors + ' errors' : '')) : '', c.score != null ? 'score ' + fmt(c.score) : '', c.ms ? ciDur(c.ms / 1000) : '', c.controller ? 'by ' + c.controller : '', c.pipeline_id ? 'pipeline ' + c.pipeline_id : '', c.delta ? (c.delta > 0 ? '+' : '') + c.delta + ' tests' : '', c.failing && c.failing.length ? '\n' + c.failing.slice(0, 6).join('\n') + (c.failing.length > 6 ? '\n+' + (c.failing.length - 6) + ' more' : '') : '', c.summary && !c.total ? '\n' + String(c.summary).slice(0, 240) : ''].filter(Boolean).join(' ');
  const ciCellHtml = (c, lane, cls) => '<i class="ci-c o-' + esc(c.o) + (cls || '') + '" style="--ag:' + ciAg(c.controller) + '" title="' + esc(ciTip(c, lane)) + '" data-ci-ref="' + esc(c.pipeline_id || c.ts || c.id) + '" data-ci-lane="' + esc(lane || '') + '"' + itemAttr(c, 'run ' + (c.id || c.ts)) + '>' + (c.failed && cls === ' big' ? '<b>' + c.failed + '</b>' : '') + '</i>';
  const ciAgents = (ctl) => { const ks = Object.keys(ctl || {}).filter((k) => ctl[k] && ctl[k].runs); if (!ks.length) return ''; const tot = ks.reduce((s, k) => s + ctl[k].runs, 0) || 1;
    return '<span class="ci-agbar" title="' + esc(ks.map((k) => k + ' ' + ctl[k].runs + ' runs · ' + ctl[k].pass + ' green · ' + ctl[k].red + ' red').join('\n')) + '">' + ks.sort((a, b) => ctl[b].runs - ctl[a].runs).map((k) => '<i style="flex:' + ctl[k].runs / tot + ';background:' + ciAg(k) + '"><em>' + esc(k) + ' ' + ctl[k].runs + '</em></i>').join('') + '</span>'; };
  const ciHead = (p, big) => { const s = p.summary || {}; const st = s.states || {};
    return '<div class="ci-hd">' + (big ? ciRing(s.pass_rate, 40) : '<b class="ci-big" style="color:' + (s.pass_rate == null ? B.t2 : s.pass_rate >= .9 ? B.ac2 : s.pass_rate >= .6 ? B.ac3 : B.ac4) + '">' + ciPct(s.pass_rate) + '</b>') + '<span class="ci-hdt"><b>' + esc(p.title || '') + '</b><small>' + [s.lanes != null ? s.lanes + ' lanes' : '', s.runs != null ? s.runs + ' runs' : '', s.shown != null && s.runs != null && s.shown < s.runs ? s.shown + ' shown' : '', s.median_attempts != null ? '~' + fmt(s.median_attempts) + ' tries to green' : '', s.median_seconds_to_green != null ? '~' + ciDur(s.median_seconds_to_green) + ' to green' : ''].filter(Boolean).join(' · ') + '</small></span>'
      + '<span class="ci-chips">' + (st.red ? '<span class="ci-chip red">' + st.red + ' red</span>' : '') + (st.racing ? '<span class="ci-chip racing">' + st.racing + ' racing</span>' : '') + (st.green ? '<span class="ci-chip green">' + st.green + ' green</span>' : '') + '</span>' + (big ? ciAgents(s.controllers) : '') + '</div>'; };
  const ciRows = (H, per, head) => Math.max(1, Math.floor((H - (head || 0)) / per));

  R['status-matrix'] = (d, H, o) => {
    const p = ciOf(d, 'status-matrix'); if (!p || !Array.isArray(p.lanes)) return EMPTY('a status matrix needs lanes of runs'); if (!p.lanes.length) return NODATA(p.source || '', 'm');
    const sz = (o && o.size) || 'm', big = sz === 'l' || sz === 'xl', per = sz === 'xl' ? 22 : big ? 19 : 16, head = sz === 'm' ? 22 : 50, n = sz === 'xl' ? p.lanes.length : ciRows(H, per, head);
    const lanes = p.lanes.slice(0, n), more = p.lanes.length - lanes.length, cls = sz === 'xl' ? ' big' : '';
    return wrap('status-matrix', ciHead(p, big) + '<div class="ci-lanes s-' + sz + (sz === 'xl' ? ' scroll' : '') + '">' + lanes.map((ln) => '<div class="ci-lane" data-ci-lane="' + esc(ln.name) + '"' + itemAttr({ lane: ln.name, state: ln.state, race: ln.race, runs: ln.cells.length + (ln.hidden || 0), pipeline_id: ln.pipeline_id, controllers: ln.controllers }, 'lane ' + ln.name) + '>'
      + '<span class="ci-ln">' + ciState(ln.state) + '<b title="' + esc(ln.name) + '">' + esc(ln.name) + '</b>' + (big ? (ln.controllers || []).map((a) => '<i class="ci-dot" style="background:' + ciAg(a) + '" title="' + esc(a) + '"></i>').join('') : '') + '</span>'
      + '<span class="ci-cells">' + (ln.hidden ? '<em class="ci-more" title="' + ln.hidden + ' older runs not drawn (ask for cols=0)">+' + ln.hidden + '</em>' : '') + ln.cells.map((c) => ciCellHtml(c, ln.name, cls)).join('') + '</span>'
      + (big ? '<span class="ci-end">' + (ln.total ? '<b>' + fmt(ln.passed) + '</b>/' + fmt(ln.total) : ln.cells.length + ' runs') + (ln.state === 'red' && ln.race && ln.race.red_runs ? '<small class="bad">' + ln.race.red_runs + ' red</small>' : (ln.race && ln.race.streak ? '<small class="ok">×' + ln.race.streak + '</small>' : '')) + '</span>' : '') + '</div>').join('')
      + (more > 0 ? '<div class="ci-foot">+' + more + ' more lanes' + (p.summary && p.summary.states && p.summary.states.red ? ' · red first with order=red' : '') + '</div>' : '') + '</div>', 'ci');
  };
  R['race-green'] = (d, H, o) => {
    const p = ciOf(d, 'race-green'); if (!p || !Array.isArray(p.lanes)) return EMPTY('a race needs lanes of runs'); if (!p.lanes.length) return NODATA(p.source || '', 'm');
    const rank = { racing: 0, red: 1, none: 2, green: 3 }; const lanes = p.view === 'race' ? p.lanes.slice() : p.lanes.slice().sort((a, b) => (rank[a.state] ?? 9) - (rank[b.state] ?? 9));
    const sz = (o && o.size) || 'm', big = sz === 'l' || sz === 'xl', per = sz === 'xl' ? 26 : big ? 22 : 18, n = sz === 'xl' ? lanes.length : ciRows(H, per, sz === 'm' ? 22 : 50), shown = lanes.slice(0, n);
    const track = (ln) => { const cs = ln.cells.slice(-(sz === 'm' ? 14 : sz === 'l' ? 24 : 60)); const L = cs.length; const r = ln.race || {}; const lap = r.last_lap;
      return '<span class="ci-trk st-' + esc(ln.state) + '"><b class="ci-rail"></b>' + cs.map((c, i) => '<i class="ci-rn o-' + esc(c.o) + (i === L - 1 ? ' last' : '') + '" style="left:calc(' + (L > 1 ? i / (L - 1) * 100 : 100) + '% - ' + (L > 1 ? i / (L - 1) * 12 : 12) + 'px);--ag:' + ciAg(c.controller) + '" title="' + esc(ciTip(c, ln.name)) + '" data-ci-ref="' + esc(c.pipeline_id || c.ts || c.id) + '" data-ci-lane="' + esc(ln.name) + '"' + itemAttr(c, 'run ' + (c.id || c.ts)) + '></i>').join('')
        + (ln.state === 'green' ? '<svg class="ci-flag" viewBox="0 0 12 14"><path d="M1 1v12" stroke="var(--b-t2)" stroke-width="1.2"/><path d="M1.6 1.5h9v6h-9z" fill="var(--b-ac2)"/><path d="M1.6 1.5h3v3h-3zM7.6 1.5h3v3h-3zM4.6 4.5h3v3h-3z" fill="var(--b-surf)"/></svg>' : '') + '</span>'
        + '<span class="ci-lap">' + (ln.state === 'green' ? (lap ? '<b class="ok">' + lap.attempts + ' tries</b>' + (lap.seconds != null ? '<small>' + ciDur(lap.seconds) + '</small>' : '') : '<b class="ok">green</b>') + (r.streak > 1 ? '<small>×' + r.streak + '</small>' : '')
          : ln.state === 'none' ? '<small>no result</small>' : '<b class="' + (ln.state === 'racing' ? 'run' : 'bad') + '">' + (r.red_runs || 0) + ' red</b>' + (r.red_since ? '<small>' + ciAgo(r.red_since) + '</small>' : '')) + '</span>'; };
    return wrap('race-green', ciHead(p, big) + '<div class="ci-race s-' + sz + (sz === 'xl' ? ' scroll' : '') + '">' + shown.map((ln) => '<div class="ci-rl" data-ci-lane="' + esc(ln.name) + '"' + itemAttr({ lane: ln.name, state: ln.state, race: ln.race }, 'lane ' + ln.name) + '><span class="ci-ln">' + ciState(ln.state) + '<b title="' + esc(ln.name) + '">' + esc(ln.name) + '</b></span>' + track(ln) + '</div>').join('')
      + (lanes.length > shown.length ? '<div class="ci-foot">+' + (lanes.length - shown.length) + ' more lanes</div>' : '') + '</div>', 'ci');
  };
  R['test-grid'] = (d, H, o) => {
    const p = ciOf(d, 'test-grid'); if (!p || !Array.isArray(p.tests)) return EMPTY('a test grid needs tests across runs'); if (!p.tests.length) { const sm = p.summary || {}; if (sm.red_runs && !sm.listed) return wrap('test-grid', '<div class="ci-allgreen"><span><b>' + sm.red_runs + ' red runs, no test names recorded</b><small>these runs kept counts only; which tests failed is recorded from this version on</small></span></div>', 'ci');
      return wrap('test-grid', '<div class="ci-allgreen">' + ciRing(1, 34) + '<span><b>No failing test in ' + (sm.runs || 0) + ' runs</b><small>' + (sm.listed < (sm.runs || 0) ? sm.listed + ' of them listed their failures' : 'every run listed its failures, and none did') + '</small></span></div>', 'ci'); }
    const sz = (o && o.size) || 'm', big = sz === 'l' || sz === 'xl', n = sz === 'xl' ? p.tests.length : ciRows(H, big ? 19 : 16, sz === 'm' ? 20 : 44), cl = (p.summary && p.summary.classes) || {};
    const cols = (p.columns || []), keep = sz === 'm' ? 18 : sz === 'l' ? 32 : cols.length;
    const off = Math.max(0, cols.length - keep);
    return wrap('test-grid', '<div class="ci-hd"><span class="ci-hdt"><b>' + esc(p.title || 'Tests across runs') + '</b><small>' + p.tests.length + ' tests · ' + ((p.summary && p.summary.runs) || cols.length) + ' runs' + (p.hidden || off ? ' · newest ' + (cols.length - off) + ' drawn' : '') + '</small></span><span class="ci-chips">' + ['broken', 'flaky', 'unknown', 'fixed'].filter((k) => cl[k]).map((k) => '<span class="ci-chip k-' + k + '">' + cl[k] + ' ' + k + '</span>').join('') + '</span></div>'
      + '<div class="ci-tg s-' + sz + (sz === 'xl' ? ' scroll' : '') + '">' + p.tests.slice(0, n).map((t) => '<div class="ci-tr" data-ci-test="' + esc(t.id) + '"' + itemAttr(t, 'test ' + t.id) + '><span class="ci-cls k-' + esc(t.class) + '">' + esc(t.class) + '</span><span class="ci-tn" title="' + esc(t.id + (t.description ? '\n' + t.description : '')) + '"><b>' + esc(t.name || t.id) + '</b>' + (big ? '<small>' + esc(t.module || '') + '</small>' : '') + '</span><span class="ci-cells">' + (t.cells || []).slice(off).map((c, i) => { const col = cols[i + off] || {}; return '<i class="ci-c t-' + (c === '-' ? 'none' : esc(c)) + '" title="' + esc((col.branch ? col.branch + ' · ' : '') + ciWhen(col.ts) + ' · ' + c) + '" data-ci-ref="' + esc(col.pipeline_id || col.ts || col.id || '') + '"></i>'; }).join('') + '</span>' + (big ? '<span class="ci-end"><b class="' + (t.fails ? 'bad' : '') + '">' + t.fails + '</b><small>fails</small></span>' : '') + '</div>').join('')
      + (p.tests.length > n ? '<div class="ci-foot">+' + (p.tests.length - n) + ' more tests</div>' : '') + '</div>', 'ci');
  };
  const CI_LANE_COL = (n) => /done|merged/.test(n) ? B.ac2 : /progress|running|now/.test(n) ? B.ac : /blocked|failed/.test(n) ? B.ac4 : /review|waiting/.test(n) ? B.ac3 : B.t3;
  R['ci-board'] = (d, H, o) => {
    const p = ciOf(d, 'ci-board'); if (!p || !Array.isArray(p.columns)) return EMPTY('a board needs columns'); if (!p.columns.length) return NODATA('', 'm');
    const sz = (o && o.size) || 'm', cols = sz === 'm' ? p.columns.filter((c) => c.items && c.items.length).slice(0, 4) : p.columns, per = sz === 'm' ? 3 : sz === 'l' ? 5 : 999;
    return wrap('ci-board', '<div class="ci-kb s-' + sz + '" style="grid-template-columns:repeat(' + Math.max(1, cols.length) + ',minmax(' + (sz === 'xl' ? 170 : 90) + 'px,1fr))">' + cols.map((c) => '<div class="ci-kc" style="--lc:' + CI_LANE_COL(String(c.name)) + '"><span class="ci-kh"><i></i>' + esc(String(c.name).replace(/_/g, ' ')) + '<b>' + (c.items || []).length + '</b></span><div class="ci-kl">' + (c.items || []).slice(0, per).map((it) => '<span class="ci-card" data-ci-ref="' + esc(it.pipeline || '') + '" data-ci-item="' + esc(it.id || '') + '"' + itemAttr(it, 'item ' + (it.id || '')) + '>'
      + '<b>' + esc(it.title || it.id || '') + '</b><span class="ci-cm">' + ciAgent(it.agent) + (it.gate ? '<i class="ci-gate g-' + esc(it.gate) + '" title="gate ' + esc(it.gate) + '"></i>' : '') + (it.calls != null ? '<small>' + it.calls + ' calls' + (it.ms ? ' · ' + ciDur(it.ms / 1000) : '') + '</small>' : '') + (sz !== 'm' && it.branch ? '<small class="mono">' + esc(it.branch) + '</small>' : '') + (it.comments ? '<small>💬' + it.comments + '</small>' : '') + '</span></span>').join('') + ((c.items || []).length > per ? '<em class="ci-more">+' + ((c.items || []).length - per) + '</em>' : '') + '</div></div>').join('') + '</div>', 'ci');
  };
  R['run-track'] = (d, H, o) => {
    const p = ciOf(d, 'run-track'); if (!p || !Array.isArray(p.stages)) return EMPTY('a run track needs stages'); const sz = (o && o.size) || 'm', pl = p.pipeline || {};
    const chev = '<div class="ci-chev">' + p.stages.map((s) => '<span class="cs cs-' + esc(s.status) + '" title="' + esc(s.name + ' · ' + s.status + (s.steps && s.steps.length ? '\n' + s.steps.map((x) => (x.ok ? '✓ ' : '✗ ') + x.stage + (x.detail ? ': ' + String(x.detail).slice(0, 200) : '')).join('\n') : '')) + '"><b>' + esc(s.name) + '</b>' + (sz !== 'm' ? '<small>' + (s.steps && s.steps.length ? s.steps.length + ' step' + (s.steps.length > 1 ? 's' : '') : s.status) + '</small>' : '') + '</span>').join('') + '</div>';
    const head = '<div class="ci-hd"><span class="ci-hdt"><b class="mono">' + esc(p.title || pl.branch || pl.id || '') + '</b><small>' + [pl.id ? 'pipeline ' + pl.id : '', pl.status, pl.decision, pl.to ? '→ ' + pl.to : '', (p.commits || []).length ? p.commits.length + ' commits' : '', (p.changed_files || []).length ? p.changed_files.length + ' files' : ''].filter(Boolean).map(esc).join(' · ') + '</small></span>' + ciAgent(p.controller) + '</div>';
    if (sz === 'm') return wrap('run-track', head + chev, 'ci');
    const steps = p.stages.flatMap((s) => (s.steps || []).map((x) => Object.assign({ at: s.name }, x)));
    return wrap('run-track', head + chev + '<div class="ci-steps' + (sz === 'xl' ? ' scroll full' : '') + '">' + steps.map((x) => '<div class="ci-step ' + (x.ok ? 'ok' : 'bad') + '"><span class="t">' + esc(ciWhen(x.ts)) + '</span><b>' + esc(x.stage) + '</b><pre>' + esc(sz === 'xl' ? x.detail : String(x.detail || '').slice(0, 160)) + '</pre></div>').join('') + (sz === 'xl' && (p.changed_files || []).length ? '<div class="ci-files">' + p.changed_files.map((f) => '<code>' + esc(typeof f === 'string' ? f : (f.path || JSON.stringify(f))) + '</code>').join('') + '</div>' : '') + '</div>', 'ci');
  };
  R['run-compare'] = (d, H, o) => {
    const p = ciOf(d, 'run-compare'); if (!p || !p.a || !p.b) return EMPTY('a comparison needs two runs'); const sz = (o && o.size) || 'm', dl = p.delta || {};
    const side = (s, k) => '<div class="ci-side o-' + esc(s.o) + '"><small>' + k + ' · ' + esc(ciWhen(s.ts)) + '</small><b>' + fmt(s.passed) + '<i>/' + fmt(s.total) + '</i></b><span>' + (s.failed ? '<em class="bad">' + s.failed + ' failed</em>' : '<em class="ok">green</em>') + ciAgent(s.controller) + '</span></div>';
    const dv = (k, good) => { const v = +dl[k] || 0; return '<span class="ci-dl ' + (v === 0 ? '' : (v > 0) === good ? 'ok' : 'bad') + '"><b>' + (v > 0 ? '+' : '') + v + '</b><small>' + k + '</small></span>'; };
    const list = (arr, cls, lbl) => arr && arr.length ? '<div class="ci-cl ' + cls + '"><span class="h">' + lbl + ' <b>' + arr.length + '</b></span>' + arr.slice(0, sz === 'xl' ? 9999 : sz === 'l' ? 6 : 3).map((t) => '<code title="' + esc(t.id + (t.description ? '\n' + t.description : '')) + '" data-ci-test="' + esc(t.id) + '">' + esc(String(t.id).split('::').pop()) + '</code>').join('') + (arr.length > (sz === 'xl' ? 9999 : sz === 'l' ? 6 : 3) ? '<em class="ci-more">+' + (arr.length - (sz === 'l' ? 6 : 3)) + '</em>' : '') + '</div>' : '';
    return wrap('run-compare', '<div class="ci-cmp">' + side(p.a, 'A') + '<span class="ci-arrow">→<small>' + esc(ciDur(p.seconds)) + '</small></span>' + side(p.b, 'B') + '</div><div class="ci-dls">' + dv('passed', true) + dv('failed', false) + dv('errors', false) + dv('total', true) + '</div><div class="ci-cmpl">' + list(p.fixed, 'ok', 'fixed') + list(p.broken, 'bad', 'broken') + list(p.still, 'warn', 'still failing') + (p.partial ? '<small class="ci-note">a run listed only some of its failures - absence proves nothing</small>' : '') + '</div>', 'ci');
  };
  R['ci-pulse'] = (d, H, o) => {
    const p = ciOf(d, 'ci-pulse') || (d && Array.isArray(d.buckets) ? { kind: 'ci', view: 'pulse', title: 'Activity', series: d.buckets.map((b) => ({ t: b.hour, runs: (+b.pass || 0) + (+b.fail || 0), pass: +b.pass || 0, red: +b.fail || 0, rate: ((+b.pass || 0) + (+b.fail || 0)) ? (+b.pass || 0) / ((+b.pass || 0) + (+b.fail || 0)) : null })), summary: {} } : null);
    if (!p || !Array.isArray(p.series)) return EMPTY('a pulse needs runs over time'); const sz = (o && o.size) || 'm', s = p.summary || {}, se = p.series.slice(sz === 'm' ? -14 : sz === 'l' ? -30 : -90);
    const hi = Math.max(1, ...se.map((x) => +x.runs || 0)), W = 100, bw = W / Math.max(1, se.length);
    // the chart draws in a fixed 100-unit space and CSS fills whatever height the tile gives it: sized from the
    // measured body it grew the body it was measured from, and a dashboard tile re-measured and re-drew until the
    // bars were 33 million px tall (the Live lens, 2026-09-27)
    const Hc = 100;
    const bars = se.map((x, i) => { const hp = (+x.pass || 0) / hi * (Hc - 4), hr = (+x.red || 0) / hi * (Hc - 4); return '<g><title>' + esc(x.t + ' · ' + x.runs + ' runs · ' + x.pass + ' green · ' + x.red + ' red' + (x.rate != null ? ' · ' + ciPct(x.rate) : '')) + '</title><rect x="' + (i * bw + bw * .15).toFixed(2) + '" y="' + (Hc - hp).toFixed(1) + '" width="' + (bw * .7).toFixed(2) + '" height="' + hp.toFixed(1) + '" rx=".6" fill="var(--b-ac2)" opacity=".85"/><rect x="' + (i * bw + bw * .15).toFixed(2) + '" y="' + (Hc - hp - hr).toFixed(1) + '" width="' + (bw * .7).toFixed(2) + '" height="' + hr.toFixed(1) + '" rx=".6" fill="var(--b-ac4)" opacity=".85"/></g>'; }).join('');
    const rl = se.map((x, i) => x.rate == null ? null : ((i + .5) * bw).toFixed(2) + ',' + (4 + (1 - x.rate) * (Hc - 8)).toFixed(1)).filter(Boolean).join(' ');
    return wrap('ci-pulse', '<div class="ci-pl">' + '<div class="ci-kpis">' + ciRing(s.pass_rate, sz === 'm' ? 40 : 52) + '<span><b>' + fmt(s.runs || se.reduce((a, x) => a + (+x.runs || 0), 0)) + '</b><small>runs</small></span>' + (s.median_seconds_to_green != null ? '<span><b>' + ciDur(s.median_seconds_to_green) + '</b><small>to green</small></span>' : '') + (s.median_attempts != null ? '<span><b>' + fmt(s.median_attempts) + '</b><small>tries</small></span>' : '') + (s.states ? '<span><b class="' + (s.states.red ? 'bad' : 'ok') + '">' + (s.states.red || 0) + '</b><small>red lanes</small></span>' : '') + '</div>'
      + '<svg class="ci-bars" viewBox="0 0 ' + W + ' ' + Hc + '" preserveAspectRatio="none">' + bars + (rl ? '<polyline points="' + rl + '" fill="none" stroke="var(--b-ac)" stroke-width="1.4" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>' : '') + '</svg>'
      + '<div class="ci-axis"><span>' + esc(String((se[0] || {}).t || '')) + '</span>' + ciAgents(s.controllers) + '<span>' + esc(String((se[se.length - 1] || {}).t || '')) + '</span></div></div>', 'ci');
  };
  R['ci-fleet'] = (d, H, o) => {
    const p = ciOf(d, 'ci-fleet'); if (!p || !Array.isArray(p.cards)) return EMPTY('a fleet needs sandboxes'); if (!p.cards.length) return NODATA('', 'm'); const sz = (o && o.size) || 'm', n = sz === 'xl' ? p.cards.length : sz === 'l' ? 9 : 6;
    return wrap('ci-fleet', '<div class="ci-fl s-' + sz + '">' + p.cards.slice(0, n).map((c) => '<span class="ci-box' + (c.running ? ' up' : '') + '" style="--ag:' + ciAg(c.owner) + '" data-ci-ref="' + esc(c.pipeline || '') + '" data-ci-lane="' + esc(c.branch || '') + '"' + itemAttr(c, 'sandbox ' + (c.name || '')) + '><span class="h"><i class="ci-up" title="' + (c.running ? 'running' : 'stopped') + '"></i><b title="' + esc(c.branch || c.name) + '">' + esc(c.branch || c.name) + '</b>' + (c.pinned ? '<small title="pinned">📌</small>' : '') + '</span><span class="ci-cm">' + ciAgent(c.owner) + (c.gate ? '<i class="ci-gate g-' + esc(c.gate) + '" title="gate ' + esc(c.gate) + '"></i>' : '') + (c.port ? '<small class="mono">:' + esc(c.port) + '</small>' : '') + (c.conversations ? '<small title="conversations">💬' + c.conversations + '</small>' : '') + (sz !== 'm' && c.last_activity ? '<small>' + esc(ciAgo(c.last_activity)) + '</small>' : '') + '</span></span>').join('') + (p.cards.length > n ? '<em class="ci-more">+' + (p.cards.length - n) + '</em>' : '') + '</div>', 'ci');
  };
  R['ci-run'] = (d, H, o) => {
    const p = ciOf(d, 'ci-run'); if (!p || (!p.cell && !p.track)) return EMPTY('a run needs a result or a pipeline'); const sz = (o && o.size) || 'm', c = p.cell || {}, fails = ((p.run && p.run.failures) || []).filter(isObj);
    const top = '<div class="ci-hd">' + '<i class="ci-c o-' + esc(c.o || 'unknown') + ' big"></i><span class="ci-hdt"><b class="mono">' + esc(p.title || '') + '</b><small>' + [c.ts ? ciWhen(c.ts) : '', c.total ? c.passed + '/' + c.total + ' passed' : '', c.failed ? c.failed + ' failed' : '', c.pipeline_id ? 'pipeline ' + c.pipeline_id : ''].filter(Boolean).map(esc).join(' · ') + '</small></span>' + ciAgent(c.controller || (p.conversation && p.conversation.controller)) + (p.conversation && p.conversation.session_id ? '<small class="ci-conv" title="conversation ' + esc(p.conversation.session_id) + '" data-ci-session="' + esc(p.conversation.session_id) + '">💬 ' + esc(String(p.conversation.session_id).slice(0, 8)) + '</small>' : '') + '</div>';
    const trk = p.track ? R['run-track'](p.track, 60, { size: 'm' }).replace(/<div class="ci-hd">[\s\S]*?<\/div>/, '') : '';
    const lane = p.lane && p.lane.cells ? '<div class="ci-lane solo"><span class="ci-ln">' + ciState(p.lane.state) + '<b>' + esc(p.lane.name) + '</b></span><span class="ci-cells">' + p.lane.cells.slice(sz === 'xl' ? 0 : -40).map((x) => ciCellHtml(x, p.lane.name, x.id === c.id ? ' sel' : '')).join('') + '</span></div>' : '';
    const fl = fails.length ? '<div class="ci-fails' + (sz === 'xl' ? ' scroll' : '') + '">' + fails.slice(0, sz === 'xl' ? 9999 : sz === 'l' ? 5 : 2).map((f) => '<div><code>' + esc(f.node_id || f.name) + '</code><span>' + esc(f.description || f.kind || '') + '</span></div>').join('') + (fails.length > (sz === 'l' ? 5 : 2) && sz !== 'xl' ? '<em class="ci-more">+' + (fails.length - (sz === 'l' ? 5 : 2)) + ' more</em>' : '') + '</div>' : '';
    const items = (p.board_items || []).length && sz !== 'm' ? '<div class="ci-its">' + p.board_items.map((it) => '<span class="ci-card mini" data-ci-item="' + esc(it.id) + '"><b>' + esc(it.title || it.id) + '</b><small>' + esc(String(it.lane || '').replace(/_/g, ' ')) + '</small></span>').join('') + '</div>' : '';
    return wrap('ci-run', top + trk + lane + fl + items, 'ci');
  };
  function ciFigure(form, data) { const p = ciOf(data && data.data !== undefined && !data.kind ? data.data : data, form); if (!p) return ''; const s = p.summary || {};
    if (form === 'test-grid') { const k = s.classes || {}; return k.broken ? k.broken + ' broken' : (k.flaky ? k.flaky + ' flaky' : (p.tests || []).length + ' tests'); }
    if (form === 'ci-board') return (p.columns || []).reduce((a, c) => a + (c.items || []).length, 0) + ' items'; if (form === 'ci-fleet') return (p.cards || []).filter((c) => c.running).length + '/' + (p.cards || []).length + ' up';
    if (form === 'run-track') { const nw = (p.stages || []).find((x) => x.status === 'now' || x.status === 'failed') || (p.stages || []).slice().reverse().find((x) => x.status === 'done'); return nw ? esc(nw.name) + ' ' + (nw.status === 'failed' ? '✗' : nw.status === 'done' ? '✓' : '…') : ''; }
    if (form === 'run-compare') return '+' + (p.fixed || []).length + ' −' + (p.broken || []).length; if (form === 'ci-run') return esc(((p.cell || {}).o) || '');
    const st = s.states || {}; return (st.red ? st.red + ' red · ' : '') + ciPct(s.pass_rate); }
  const CI_CSS = '.vb-status-matrix,.vb-race-green,.vb-test-grid,.vb-ci-board,.vb-run-track,.vb-run-compare,.vb-ci-pulse,.vb-ci-fleet,.vb-ci-run{display:flex;flex-direction:column;gap:6px;min-height:0;min-width:0;height:100%}'
    + '.ci .mono,.ci code{font-family:var(--b-mono)}.ci .ok{color:var(--b-ac2)}.ci .bad{color:var(--b-ac4)}.ci .run{color:var(--b-ac)}.ci .scroll{overflow:auto;scrollbar-width:thin}'
    + '.ci-hd{display:flex;align-items:center;gap:8px;flex:none;min-width:0}.ci-big{font:600 18px/1 var(--b-mono);letter-spacing:-.02em}.ci-hdt{display:flex;flex-direction:column;min-width:0;flex:1}.ci-hdt b{font-size:11.5px;font-weight:600;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ci-hdt small{font-size:9.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.ci-chips{display:flex;gap:4px;flex:none}.ci-chip{font:500 9.5px/1 var(--b-ui);padding:3px 7px;border-radius:999px;background:var(--b-surf2);color:var(--b-t2);white-space:nowrap}.ci-chip.red,.ci-chip.k-broken{background:' + mix(B.ac4, 20) + ';color:var(--b-ac4)}.ci-chip.green,.ci-chip.k-fixed{background:' + mix(B.ac2, 18) + ';color:var(--b-ac2)}.ci-chip.racing{background:' + mix(B.ac, 20) + ';color:var(--b-ac)}.ci-chip.k-flaky{background:' + mix(B.ac3, 20) + ';color:var(--b-ac3)}'
    + '.ci-ring text{font:600 10px var(--b-mono);fill:var(--b-t1)}.ci-ring{flex:none}'
    + '.ci-agbar{display:flex;height:14px;min-width:90px;max-width:220px;flex:1;border-radius:4px;overflow:hidden;gap:1px}.ci-agbar i{display:flex;align-items:center;min-width:3px;overflow:hidden}.ci-agbar em{font:500 8.5px var(--b-ui);color:var(--b-on,#111);padding:0 4px;white-space:nowrap;font-style:normal;opacity:.9}'
    + '.ci-st{width:8px;height:8px;border-radius:50%;flex:none;background:var(--b-t3)}.ci-st.st-green{background:var(--b-ac2);box-shadow:0 0 0 2px ' + mix(B.ac2, 25) + '}.ci-st.st-red{background:var(--b-ac4);box-shadow:0 0 0 2px ' + mix(B.ac4, 25) + '}.ci-st.st-racing{background:var(--b-ac);animation:ciPulse 1.2s ease-in-out infinite}'
    + '@keyframes ciPulse{0%,100%{box-shadow:0 0 0 0 ' + mix(B.ac, 60) + '}50%{box-shadow:0 0 0 4px transparent}}@keyframes ciRun{0%{background-position:0 0}100%{background-position:12px 0}}'
    + '.ci-lanes,.ci-race,.ci-tg{display:flex;flex-direction:column;gap:3px;min-height:0;flex:1}.ci-lane,.ci-rl,.ci-tr{display:grid;grid-template-columns:minmax(80px,28%) minmax(0,1fr) auto;align-items:center;gap:8px;min-width:0;border-radius:5px;padding:1px 3px}.ci-lane:hover,.ci-rl:hover,.ci-tr:hover{background:var(--b-surf2)}.s-m .ci-lane,.s-m .ci-rl,.s-m .ci-tr{grid-template-columns:minmax(64px,34%) minmax(0,1fr)}'
    + '.ci-ln{display:flex;align-items:center;gap:5px;min-width:0}.ci-ln b{font:500 10.5px var(--b-mono);color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ci-dot{width:5px;height:5px;border-radius:50%;flex:none}'
    + '.ci-cells{display:flex;justify-content:flex-end;align-items:center;gap:2px;overflow:hidden;min-width:0}.ci-more{font:500 9px var(--b-mono);color:var(--b-t3);font-style:normal;padding:0 3px;flex:none}'
    + '.ci-c{flex:none;width:9px;height:12px;border-radius:2px;background:var(--b-surf3);position:relative;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:inset 0 -2px 0 var(--ag,transparent);transition:transform .12s}.ci-c:hover{transform:scale(1.35);z-index:1}.s-l .ci-c{width:11px;height:14px}.s-xl .ci-c,.ci-c.big{width:14px;height:17px}.ci-c b{font:600 8px var(--b-mono);color:#fff}'
    + '.ci-c.o-pass,.ci-c.t-pass{background:var(--b-ac2)}.ci-c.o-fail,.ci-c.t-fail{background:var(--b-ac4)}.ci-c.o-error,.ci-c.t-error{background:repeating-linear-gradient(135deg,var(--b-ac4) 0 3px,' + mix(B.ac4, 55) + ' 3px 6px)}.ci-c.o-warn{background:var(--b-ac3)}.ci-c.o-running{background:repeating-linear-gradient(90deg,var(--b-ac) 0 6px,' + mix(B.ac, 45) + ' 6px 12px);background-size:12px 100%;animation:ciRun .8s linear infinite}.ci-c.o-skip{background:var(--b-t3);opacity:.5}.ci-c.o-unknown,.ci-c.t-unknown{background:' + mix(B.t3, 35) + '}.ci-c.t-none{background:transparent;box-shadow:inset 0 0 0 1px var(--b-bd)}.ci-c.sel{outline:2px solid var(--b-t1);outline-offset:1px}'
    + '.ci-end{display:flex;align-items:baseline;gap:4px;font:400 9.5px var(--b-mono);color:var(--b-t3);white-space:nowrap}.ci-end b{color:var(--b-t1);font-weight:600}.ci-end small{font-size:9px}'
    + '.ci-foot{font-size:9.5px;color:var(--b-t3);padding:2px 4px}'
    + '.ci-trk{position:relative;height:14px;min-width:0;margin-right:14px}.ci-rail{position:absolute;left:0;right:-10px;top:50%;height:2px;margin-top:-1px;border-radius:2px;background:linear-gradient(90deg,' + mix(B.t3, 25) + ',' + mix(B.t3, 45) + ')}.ci-trk.st-green .ci-rail{background:linear-gradient(90deg,' + mix(B.ac4, 40) + ',' + mix(B.ac2, 80) + ')}.ci-trk.st-red .ci-rail,.ci-trk.st-racing .ci-rail{background:linear-gradient(90deg,' + mix(B.t3, 30) + ',' + mix(B.ac4, 70) + ')}'
    + '.ci-rn{position:absolute;top:50%;width:12px;height:12px;margin-top:-6px;border-radius:50%;background:var(--b-surf3);box-shadow:0 0 0 2px var(--b-surf);cursor:pointer}.ci-rn.o-pass{background:var(--b-ac2)}.ci-rn.o-fail,.ci-rn.o-error{background:var(--b-ac4)}.ci-rn.o-warn{background:var(--b-ac3)}.ci-rn.o-running{background:var(--b-ac);animation:ciPulse 1.2s infinite}.ci-rn:not(.last){transform:scale(.62)}.ci-rn.last{box-shadow:0 0 0 2px var(--b-surf),0 0 0 4px var(--ag,transparent)}.ci-rn:hover{transform:scale(1.2);z-index:2}'
    + '.ci-flag{position:absolute;right:-16px;top:-3px;width:12px;height:16px}.ci-lap{display:flex;align-items:baseline;gap:4px;font:600 10px var(--b-mono);white-space:nowrap}.ci-lap small{font-weight:400;color:var(--b-t3);font-size:9px}.s-m .ci-lap{display:none}'
    + '.ci-cls{display:inline-block;font:600 8px/1 var(--b-ui);text-transform:uppercase;letter-spacing:.06em;padding:3px 5px;border-radius:3px;background:var(--b-surf2);color:var(--b-t3);text-align:center;min-width:44px}.ci-cls.k-broken{color:var(--b-ac4);background:' + mix(B.ac4, 16) + '}.ci-cls.k-flaky{color:var(--b-ac3);background:' + mix(B.ac3, 16) + '}.ci-cls.k-fixed{color:var(--b-ac2);background:' + mix(B.ac2, 14) + '}'
    + '.ci-tr{grid-template-columns:auto minmax(80px,30%) minmax(0,1fr) auto}.s-m .ci-tr{grid-template-columns:auto minmax(60px,34%) minmax(0,1fr)}.ci-tn{display:flex;flex-direction:column;min-width:0}.ci-tn b{font:500 10px var(--b-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--b-t1)}.ci-tn small{font:9px var(--b-mono);color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.ci-allgreen{display:flex;align-items:center;gap:10px;height:100%}.ci-allgreen span{display:flex;flex-direction:column}.ci-allgreen b{font-size:12px}.ci-allgreen small{font-size:10px;color:var(--b-t3)}'
    + '.ci-kb{display:grid;gap:6px;flex:1;min-height:0;overflow-x:auto}.ci-kc{display:flex;flex-direction:column;gap:4px;min-width:0;min-height:0;background:var(--b-surf2);border-radius:7px;padding:6px;border-top:2px solid var(--lc)}.ci-kh{display:flex;align-items:center;gap:5px;font:600 8.5px var(--b-ui);text-transform:uppercase;letter-spacing:.08em;color:var(--b-t2)}.ci-kh i{width:6px;height:6px;border-radius:50%;background:var(--lc)}.ci-kh b{margin-left:auto;font:400 9px var(--b-mono);color:var(--b-t3)}.ci-kl{display:flex;flex-direction:column;gap:4px;min-height:0;overflow:auto;scrollbar-width:none}'
    + '.ci-card{display:flex;flex-direction:column;gap:3px;background:var(--b-surf);border-radius:5px;padding:5px 7px;box-shadow:0 1px 0 var(--b-bd),inset 2px 0 0 var(--lc,var(--b-ac));cursor:pointer;min-width:0}.ci-card:hover{box-shadow:0 0 0 1px var(--b-ac),inset 2px 0 0 var(--lc,var(--b-ac))}.ci-card>b{font-size:10px;font-weight:500;color:var(--b-t1);line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.s-xl .ci-card>b{-webkit-line-clamp:unset;display:block}'
    + '.ci-cm{display:flex;align-items:center;gap:5px;flex-wrap:wrap;min-width:0}.ci-cm small{font-size:8.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}'
    + '.ci-ag{font:600 8px/1 var(--b-ui);text-transform:uppercase;letter-spacing:.06em;padding:2px 5px;border-radius:3px;color:var(--ag);background:color-mix(in srgb,var(--ag) 16%,transparent);white-space:nowrap}'
    + '.ci-gate{width:7px;height:7px;border-radius:50%;background:var(--b-t3)}.ci-gate.g-pass{background:var(--b-ac2)}.ci-gate.g-fail{background:var(--b-ac4)}'
    + '.ci-chev{display:flex;gap:2px;flex:none}.ci-chev .cs{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;padding:6px 4px 6px 10px;background:var(--b-surf2);clip-path:polygon(0 0,calc(100% - 7px) 0,100% 50%,calc(100% - 7px) 100%,0 100%,7px 50%);min-width:0}.ci-chev .cs:first-child{clip-path:polygon(0 0,calc(100% - 7px) 0,100% 50%,calc(100% - 7px) 100%,0 100%);padding-left:6px}.ci-chev b{font:600 9.5px var(--b-ui);color:var(--b-t2);white-space:nowrap}.ci-chev small{font-size:8.5px;color:var(--b-t3)}'
    + '.ci-chev .cs-done{background:' + mix(B.ac2, 24) + '}.ci-chev .cs-done b{color:var(--b-ac2)}.ci-chev .cs-failed{background:' + mix(B.ac4, 26) + '}.ci-chev .cs-failed b{color:var(--b-ac4)}.ci-chev .cs-now{background:repeating-linear-gradient(90deg,' + mix(B.ac, 30) + ' 0 8px,' + mix(B.ac, 18) + ' 8px 16px);background-size:16px 100%;animation:ciRun 1s linear infinite}.ci-chev .cs-now b{color:var(--b-ac)}'
    + '.ci-steps{display:flex;flex-direction:column;gap:3px;min-height:0;flex:1;overflow:hidden}.ci-step{display:grid;grid-template-columns:auto auto minmax(0,1fr);gap:6px;align-items:baseline;font-size:9.5px;padding:2px 4px;border-left:2px solid var(--b-ac2);border-radius:2px}.ci-step.bad{border-left-color:var(--b-ac4)}.ci-step .t{font:9px var(--b-mono);color:var(--b-t3)}.ci-step b{font-weight:600;color:var(--b-t2)}.ci-step pre{margin:0;font:9.5px/1.35 var(--b-mono);color:var(--b-t1);white-space:pre-wrap;word-break:break-word;max-height:2.8em;overflow:hidden}.ci-steps.full pre{max-height:none}'
    + '.ci-files{display:flex;flex-wrap:wrap;gap:4px;padding-top:4px}.ci-files code{font-size:9px;padding:1px 5px;border-radius:3px;background:var(--b-surf2);color:var(--b-t2)}'
    + '.ci-cmp{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;flex:none}.ci-side{display:flex;flex-direction:column;gap:2px;padding:6px 8px;border-radius:7px;background:var(--b-surf2);border-left:3px solid var(--b-t3)}.ci-side.o-pass{border-left-color:var(--b-ac2)}.ci-side.o-fail,.ci-side.o-error{border-left-color:var(--b-ac4)}.ci-side small{font-size:9px;color:var(--b-t3)}.ci-side>b{font:600 15px var(--b-mono)}.ci-side>b i{font-style:normal;font-size:10px;color:var(--b-t3);font-weight:400}.ci-side span{display:flex;gap:5px;align-items:center;font-size:9.5px}.ci-side em{font-style:normal}'
    + '.ci-arrow{display:flex;flex-direction:column;align-items:center;font-size:16px;color:var(--b-t3)}.ci-arrow small{font:9px var(--b-mono)}.ci-dls{display:flex;gap:6px;flex:none}.ci-dl{flex:1;display:flex;flex-direction:column;align-items:center;padding:3px;border-radius:5px;background:var(--b-surf2)}.ci-dl b{font:600 12px var(--b-mono)}.ci-dl small{font-size:8.5px;color:var(--b-t3)}.ci-dl.ok b{color:var(--b-ac2)}.ci-dl.bad b{color:var(--b-ac4)}'
    + '.ci-cmpl{display:flex;flex-direction:column;gap:4px;min-height:0;overflow:auto;flex:1}.vb-ci-run .vb-run-track{height:auto;flex:none}.ci-cl{display:flex;flex-wrap:wrap;gap:3px;align-items:center}.ci-cl .h{font:600 9px var(--b-ui);text-transform:uppercase;letter-spacing:.06em;margin-right:4px}.ci-cl.ok .h{color:var(--b-ac2)}.ci-cl.bad .h{color:var(--b-ac4)}.ci-cl.warn .h{color:var(--b-ac3)}.ci-cl code{font-size:9px;padding:1px 5px;border-radius:3px;background:var(--b-surf2);color:var(--b-t1);cursor:default}.ci-note{font-size:9px;color:var(--b-ac3)}'
    + '.ci-pl{display:flex;flex-direction:column;gap:6px;height:100%;min-height:0}.ci-kpis{display:flex;align-items:center;gap:14px;flex:none}.ci-kpis span{display:flex;flex-direction:column}.ci-kpis b{font:600 15px/1.1 var(--b-mono);color:var(--b-t1)}.ci-kpis small{font-size:9px;color:var(--b-t3);text-transform:uppercase;letter-spacing:.06em}.ci-bars{width:100%;display:block;flex:1 1 0;min-height:48px;height:auto}.ci-axis{display:flex;align-items:center;gap:8px;font:9px var(--b-mono);color:var(--b-t3);flex:none}.ci-axis .ci-agbar{flex:1;max-width:none}'
    + '.ci-fl{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:5px;align-content:start;min-height:0;overflow:auto;flex:1}.ci-box{display:flex;flex-direction:column;gap:4px;padding:6px 8px;border-radius:7px;background:var(--b-surf2);box-shadow:inset 0 2px 0 var(--ag);cursor:pointer;min-width:0;opacity:.72}.ci-box.up{opacity:1}.ci-box:hover{box-shadow:inset 0 2px 0 var(--ag),0 0 0 1px var(--b-ac)}.ci-box .h{display:flex;align-items:center;gap:5px;min-width:0}.ci-box .h b{font:500 10px var(--b-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--b-t1)}.ci-up{width:7px;height:7px;border-radius:50%;background:var(--b-t3);flex:none}.ci-box.up .ci-up{background:var(--b-ac2);box-shadow:0 0 6px var(--b-ac2)}'
    + '.ci-lane.solo{grid-template-columns:minmax(60px,22%) minmax(0,1fr)}.ci-fails{display:flex;flex-direction:column;gap:3px;min-height:0}.ci-fails>div{display:grid;grid-template-columns:minmax(0,auto) minmax(0,1fr);gap:8px;align-items:baseline;padding:3px 6px;border-radius:4px;background:' + mix(B.ac4, 9) + ';border-left:2px solid var(--b-ac4)}.ci-fails code{font-size:9.5px;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46ch}.ci-fails span{font-size:9.5px;color:var(--b-t2);white-space:pre-wrap;word-break:break-word}.ci-its{display:flex;flex-wrap:wrap;gap:4px}.ci-card.mini{flex-direction:row;align-items:baseline;gap:6px;padding:3px 7px}.ci-conv{font-size:9.5px;color:var(--b-t2);cursor:pointer}';


  /* CENSUS × COMMITS: what the census measured, run by run, beside what landed on main before each run (ci.census).
     One column a run, oldest to newest: its goals done (green) and capped (red) as a bar, a mark over it for its
     change against the run before - ▲/▼ when the change clears the measured noise floor, a dot when it does not -
     the commits that landed before it as a stack (a ring is a merge), and each goal's outcome in its row. A column
     whose change was a real improvement is lit green, a real regression red: the commits in a lit column are the
     ones to read. XL lists the signals with the commits that preceded each. */
  R['census-commits'] = (d, H, o) => {
    const p = (d && d.kind === 'ci' && d.view === 'census') ? d : null; if (!p) return EMPTY('census × commits reads ci.census');
    const all = p.runs || []; if (!all.length) return NODATA('ci.census', 'm');
    const sz = (o && o.size) || 'm', big = sz === 'l' || sz === 'xl';
    const keep = sz === 'm' ? 20 : sz === 'l' ? 36 : all.length, off = Math.max(0, all.length - keep), runs = all.slice(off);
    const s = p.summary || {}, gmax = Math.max(1, ...runs.map((r) => r.goals || 0));
    const col = (r) => r.verdict === 'signal' ? (r.direction === 'up' ? ' up' : r.direction === 'down' ? ' dn' : '') : '';
    const mark = (r) => r.verdict == null ? '' : r.verdict === 'signal' ? (r.direction === 'up' ? '▲' : r.direction === 'down' ? '▼' : '◆') : '·';
    const tip = (r) => [r.id, r.done + '/' + r.goals + ' done' + (r.capped ? ' · ' + r.capped + ' capped' : ''), Math.round((r.wall_s || 0) / 60) + ' min wall', r.quality != null ? 'quality ' + fmt(r.quality) : '',
      r.delta_done != null ? ((r.delta_done > 0 ? '+' : '') + r.delta_done + ' done, ' + (r.delta_wall > 0 ? '+' : '') + Math.round((r.delta_wall || 0) * 100) + '% wall · ' + (r.verdict === 'signal' ? 'SIGNAL' : 'within noise')) : '',
      r.commit_count ? r.commit_count + ' commits landed before it' + (r.merges ? ' (' + r.merges + ' merges)' : '') : 'nothing landed before it',
      (r.commits || []).slice(0, 8).map((c) => (c.merge ? '⭘ ' : '• ') + c.subject).join('\n')].filter(Boolean).join('\n');
    const BH = sz === 'm' ? 40 : sz === 'l' ? 56 : 84, cmax = sz === 'm' ? 4 : 7;
    const cols = runs.map((r) => {
      const dh = Math.round((r.done || 0) / gmax * BH), ch = Math.round((r.capped || 0) / gmax * BH);
      const dots = (r.commits || []).slice(0, cmax).map((c) => '<i class="' + (c.merge ? 'mg' : '') + (c.during_run ? ' dr' : '') + '"></i>').join('') + (r.commit_count > cmax ? '<em>+' + (r.commit_count - cmax) + '</em>' : '');
      return '<div class="cc-col' + col(r) + '" title="' + esc(tip(r)) + '"' + itemAttr({ run: r.id, done: r.done, goals: r.goals, verdict: r.verdict, direction: r.direction, delta_done: r.delta_done, delta_wall: r.delta_wall, commits: (r.commits || []).map((c) => c.sha + ' ' + c.subject) }, 'run ' + r.id) + ' data-ci-ref="' + esc(r.id) + '">'
        + '<span class="cc-mk">' + mark(r) + '</span><span class="cc-bar" style="height:' + BH + 'px"><i class="cp" style="height:' + ch + 'px"></i><i class="dn" style="height:' + dh + 'px"></i></span>'
        + '<span class="cc-cm">' + dots + '</span></div>';
    }).join('');
    const goals = big ? (p.goals || []) : [];
    const grid = goals.length ? '<div class="cc-goals">' + goals.map((g) => '<div class="cc-gr"><span class="n" title="' + esc(g.id) + '">' + esc(g.name) + '</span><span class="cc-gc" style="grid-template-columns:repeat(' + runs.length + ',1fr)">' + (g.cells || []).slice(off).map((c, i) => '<i class="g-' + (c || 'none') + '" title="' + esc(g.name + ' · ' + runs[i].id + ' · ' + (c || 'not run')) + '"></i>').join('') + '</span><span class="v">' + g.pass + '/' + g.runs + '</span></div>').join('') + '</div>' : '';
    const sig = sz === 'xl' ? all.filter((r) => r.verdict === 'signal').slice(-8).reverse() : [];
    const sigs = sig.length ? '<div class="cc-sigs">' + sig.map((r) => '<div class="cc-sg ' + (r.direction === 'up' ? 'up' : 'dn') + '"' + itemAttr({ run: r.id, commits: (r.commits || []).map((c) => c.sha + ' ' + c.subject) }, 'run ' + r.id) + '><b>' + (r.direction === 'up' ? '▲' : '▼') + ' ' + esc(r.id) + '</b><small>' + (r.delta_done > 0 ? '+' : '') + r.delta_done + ' done · ' + (r.delta_wall > 0 ? '+' : '') + Math.round((r.delta_wall || 0) * 100) + '% wall · ' + r.commit_count + ' commits before it</small>' + (r.commits || []).slice(0, 4).map((c) => '<code>' + esc(c.sha) + '</code><span>' + esc(c.subject) + '</span>').join('') + '</div>').join('') + '</div>' : '';
    return wrap('census-commits', '<div class="ci-hd"><span class="ci-hdt"><b>' + esc(p.title || 'Census × commits') + ' · ' + esc(p.template || '') + '</b><small>' + (s.runs || 0) + ' runs · latest ' + esc(s.latest || '') + ' ' + (s.latest_done != null ? s.latest_done + '/' + (s.goals || '') : '') + ' · ' + (s.commits || 0) + ' commits · ' + (s.signals_up || 0) + ' real gains, ' + ((s.signals || 0) - (s.signals_up || 0)) + ' real losses · noise = ±1 goal / 13% wall' + (off ? ' · newest ' + runs.length + ' drawn' : '') + '</small></span></div>'
      + '<div class="cc-plot"><div class="cc-lbl"><span style="height:14px"></span><span style="height:' + BH + 'px">done</span><span>landed</span></div><div class="cc-cols" style="grid-template-columns:repeat(' + runs.length + ',1fr)">' + cols + '</div></div>'
      + grid + sigs, 'ci');
  };
  /* A LOOP LAB ELEMENT, AS A WIDGET: the page's own custom elements (the commit graph, the author map, the test
     activity, the error radar, the branch pipeline, the task matrix, the bench compare, the routing map, the CI
     command centre) mounted into the widget's slot - so each is a tile a dashboard, the canvas or a lens can place,
     drawn by the element that already draws it on the page. draw.tag names it (draw.attrs its attributes); the
     script is fetched once from /ui/elements/. At XS/S it says what it is. */
  const EL_SRC = { 'vera-git-graph': 'git_graph', 'vera-author-map': 'author_map', 'vera-node-workers': 'node_workers', 'vera-test-activity-timeline': 'test_activity_timeline', 'vera-error-radar': 'error_radar',
    'vera-branch-pipeline': 'branch_pipeline', 'vera-task-matrix': 'task_matrix', 'vera-bench-compare': 'bench_compare', 'vera-ollama-map': 'ollama_map', 'vera-ci-ops': 'ci_ops', 'vera-agent-loop-output': 'agent_loop_output' };
  const EL_NAME = { 'vera-git-graph': 'commit graph', 'vera-author-map': 'authorship', 'vera-node-workers': 'node workers', 'vera-test-activity-timeline': 'test activity', 'vera-error-radar': 'error radar', 'vera-branch-pipeline': 'branch pipeline', 'vera-task-matrix': 'task matrix', 'vera-bench-compare': 'bench compare', 'vera-ollama-map': 'routing map', 'vera-ci-ops': 'CI command centre' };
  const elOf = (d, o) => { const dr = (o && o.draw) || (o && o.record && o.record.draw) || {}; const tag = String(dr.tag || (d && d.tag) || '').toLowerCase(); return { tag, attrs: dr.attrs || (d && d.attrs) || {} }; };
  R.element = (d, H, o) => { const e = elOf(d, o); if (!/^vera-[a-z0-9-]+$/.test(e.tag)) return wrap('element', '<div class="vb-el-pick"><b>A page element</b><span>draw.tag names it:</span>' + Object.keys(EL_NAME).map((t) => '<code>' + esc(t) + '</code><i>' + esc(EL_NAME[t]) + '</i>').join('') + '</div>');
    return wrap('element', '<div class="vb-el" style="height:' + Math.max(90, (H || 200) - 4) + 'px"><slot name="el"><span class="vb-lbl">' + esc(EL_NAME[e.tag] || e.tag) + '</span></slot></div>'); };
  function mountElement(el, rec) {
    const e = elOf(null, { record: rec }); if (!/^vera-[a-z0-9-]+$/.test(e.tag)) return;
    const sig = e.tag + '|' + JSON.stringify(e.attrs);
    if (el._elHost && el._elSig === sig) return;
    if (el._elHost) { el._elHost.remove(); el._elHost = null; }
    const host = document.createElement('div'); host.setAttribute('slot', 'el'); host.style.cssText = 'width:100%;height:100%;overflow:auto;min-height:0';
    const go = () => { const x = document.createElement(e.tag); Object.keys(e.attrs || {}).forEach((k) => x.setAttribute(k, String(e.attrs[k]))); x.style.display = 'block'; host.appendChild(x); };
    const base = (el.base || '').replace(/\/$/, ''), src = EL_SRC[e.tag];
    if (window.customElements && customElements.get(e.tag)) go();
    else if (src) { let sc = document.querySelector('script[data-vw-el="' + e.tag + '"]'); if (!sc) { sc = document.createElement('script'); sc.src = base + '/ui/elements/' + src + '.js'; sc.setAttribute('data-vw-el', e.tag); document.head.appendChild(sc); }
      if (window.customElements && customElements.whenDefined) customElements.whenDefined(e.tag).then(go); else sc.addEventListener('load', go, { once: true }); }
    el.appendChild(host); el._elHost = host; el._elSig = sig;
  }
  Object.assign(DRAWN, { 'census-commits': 'matrix', element: 'panel' });
  Object.assign(CI_GLYPH, { 'census-commits': '⨯' });
  Object.assign(FORM_SAMPLE, {
    'census-commits': () => { const runs = Array.from({ length: 14 }, (_, i) => { const done = [9, 10, 9, 11, 10, 12, 11, 10, 12, 12, 11, 10, 12, 11][i]; return { id: 'run' + (64 + i), goals: 12, done, capped: 12 - done, wall_s: 8000 + (i % 4) * 700, commit_count: [2, 0, 5, 3, 0, 9, 1, 0, 6, 2, 0, 4, 11, 1][i], merges: i % 3, commits: Array.from({ length: [2, 0, 5, 3, 0, 9, 1, 0, 6, 2, 0, 4, 11, 1][i] }, (_, k) => ({ sha: 'c' + i + k, subject: k % 2 ? 'Loop Lab: merge feat/x' + k : 'loop: a fix ' + k, merge: k % 2 === 1 })) }; });
      runs.forEach((r, i) => { if (i) { const pr = runs[i - 1]; r.delta_done = r.done - pr.done; r.delta_wall = (r.wall_s - pr.wall_s) / pr.wall_s; r.verdict = Math.abs(r.delta_done) > 1 ? 'signal' : 'noise'; r.direction = r.delta_done > 0 ? 'up' : r.delta_done < 0 ? 'down' : 'flat'; } });
      const goals = ['trivial-chat', 'research-web', 'build-multifile', 'long-horizon', 'operate-exec', 'analyse-data'].map((n, gi) => ({ id: 'census-default-' + n, name: n, cells: runs.map((r, i) => ((i + gi) % 5 === 0 ? 'cap' : 'pass')), pass: 11, runs: 14 }));
      return { kind: 'ci', view: 'census', title: 'Census × commits', template: 'default', runs, goals, summary: { runs: 14, latest: 'run77', latest_done: 11, goals: 12, commits: 44, signals: 4, signals_up: 3 } }; },
    element: () => ({ tag: 'vera-git-graph' }),
  });
  const CC_CSS = '.vb-census-commits{display:flex;flex-direction:column;gap:8px;min-height:0;height:100%;overflow:auto}'
    + '.cc-plot{display:grid;grid-template-columns:54px minmax(0,1fr);gap:6px;align-items:end}.cc-lbl{display:flex;flex-direction:column;gap:3px;font:9px var(--b-mono);color:var(--b-t3);text-align:right}.cc-lbl span{display:flex;align-items:flex-end;justify-content:flex-end}'
    + '.cc-cols{display:grid;gap:3px;align-items:end}.cc-col{display:flex;flex-direction:column;align-items:center;gap:3px;border-radius:5px;padding:2px 0;cursor:pointer;min-width:0}.cc-col:hover{background:var(--b-surf2)}'
    + '.cc-col.up{background:' + mix(B.ac2, 13) + ';box-shadow:inset 0 -2px 0 var(--b-ac2)}.cc-col.dn{background:' + mix(B.ac4, 13) + ';box-shadow:inset 0 -2px 0 var(--b-ac4)}'
    + '.cc-mk{font-size:9px;line-height:12px;height:12px;color:var(--b-t3)}.cc-col.up .cc-mk{color:var(--b-ac2)}.cc-col.dn .cc-mk{color:var(--b-ac4)}'
    + '.cc-bar{width:70%;max-width:14px;display:flex;flex-direction:column;justify-content:flex-end;background:var(--b-surf2);border-radius:3px;overflow:hidden}.cc-bar .dn{background:var(--b-ac2)}.cc-bar .cp{background:var(--b-ac4);opacity:.85}'
    + '.cc-cm{display:flex;flex-direction:column-reverse;align-items:center;gap:2px;min-height:14px}.cc-cm i{width:6px;height:6px;border-radius:50%;background:var(--b-dv1)}.cc-cm i.mg{background:transparent;box-shadow:inset 0 0 0 1.5px var(--b-dv2);width:7px;height:7px}.cc-cm i.dr{outline:1px dashed var(--b-ac3)}.cc-cm em{font:8px var(--b-mono);color:var(--b-t3);font-style:normal}'
    + '.cc-goals{display:flex;flex-direction:column;gap:2px}.cc-gr{display:grid;grid-template-columns:54px minmax(0,1fr) 40px;gap:6px;align-items:center}.cc-gr .n{font:9px var(--b-mono);color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:right}.cc-gr .v{font:9px var(--b-mono);color:var(--b-t3)}'
    + '.cc-gc{display:grid;gap:3px}.cc-gc i{height:9px;border-radius:2px;background:var(--b-surf2)}.cc-gc i.g-pass{background:var(--b-ac2)}.cc-gc i.g-cap{background:var(--b-ac3)}.cc-gc i.g-fail{background:var(--b-ac4)}.cc-gc i.g-none{background:transparent;box-shadow:inset 0 0 0 1px var(--b-bd)}'
    + '.cc-sigs{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px}.cc-sg{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 8px;padding:7px 9px;border-radius:7px;background:var(--b-surf2);border-left:3px solid var(--b-ac2);cursor:pointer}.cc-sg.dn{border-left-color:var(--b-ac4)}.cc-sg b{grid-column:1/-1;font:600 11px var(--b-mono)}.cc-sg small{grid-column:1/-1;font-size:9.5px;color:var(--b-t3);margin-bottom:3px}.cc-sg code{font:9px var(--b-mono);color:var(--b-t3)}.cc-sg span{font-size:10px;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.vb-el-pick{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:10px;color:var(--b-t2)}.vb-el-pick b,.vb-el-pick span{grid-column:1/-1}.vb-el-pick code{font:9.5px var(--b-mono);color:var(--b-t1)}.vb-el-pick i{font-style:normal;color:var(--b-t3)}.vb-element{height:100%;min-height:0}.vb-el{position:relative;width:100%;overflow:hidden;border-radius:var(--b-r)}.vb-el>slot{display:block;width:100%;height:100%}';


  /* AGENTIC LOOP PERFORMANCE (loop.ci.perf): one row a loop run, newest first - its outcome, its wall time against
     the slowest shown, where its tool time went (a stacked bar, a colour per tool, the same colour everywhere in the
     face), its calls and model calls; XL adds each run's model calls by loop stage and its warnings. A row opens the
     loop (the item drawer carries its session id). */
  const LP_COL = {};
  const lpCol = (tool, i) => LP_COL[tool] || (LP_COL[tool] = DV(Object.keys(LP_COL).length + 1));
  R['loop-perf'] = (d, H, o) => {
    const p = (d && d.kind === 'ci' && d.view === 'loop-perf') ? d : null; if (!p) return EMPTY('loop performance reads loop.ci.perf');
    const rows = p.rows || []; if (!rows.length) return NODATA('loop.ci.perf', 'm');
    const sz = (o && o.size) || 'm', s = p.summary || {}, n = sz === 'xl' ? rows.length : sz === 'l' ? 12 : 6;
    const wmax = Math.max(1, ...rows.map((r) => r.wall_s || 0));
    (s.tools || []).forEach((t) => lpCol(t.tool));
    const dur = (x) => x == null ? '—' : x < 90 ? Math.round(x) + 's' : x < 5400 ? Math.round(x / 60) + 'm' : (Math.round(x / 360) / 10) + 'h';
    const kp = (v, l, cls) => '<span><b class="' + (cls || '') + '">' + v + '</b><small>' + l + '</small></span>';
    const head = '<div class="lp-kpis">' + ciRing(s.pass_rate, sz === 'm' ? 34 : 42) + kp(s.runs || 0, 'runs') + kp(dur(s.wall_median), 'median') + kp(dur(s.wall_p90), 'p90', 'warn') + kp(fmt(s.calls_median || 0), 'tool calls') + kp(fmt(s.llm_median || 0), 'model calls')
      + (big(sz) ? '<span class="lp-leg">' + (s.tools || []).slice(0, 6).map((t) => '<i style="--c:' + lpCol(t.tool) + '"></i>' + esc(t.tool.replace(/^[a-z]+\./, '')) + ' <em>' + dur(t.ms / 1000) + '</em>').join('') + '</span>' : '') + '</div>';
    const body = rows.slice(0, n).map((r) => {
      const tot = Math.max(1, r.tool_ms || 0);
      const stack = (r.by_tool || []).map((t) => '<i style="width:' + (t.ms / tot * 100).toFixed(1) + '%;background:' + lpCol(t.tool) + '" title="' + esc(t.tool + ' · ' + dur(t.ms / 1000)) + '"></i>').join('');
      const stages = sz === 'xl' && (r.stages || []).length ? '<div class="lp-st">' + r.stages.map((x) => '<span title="' + esc(x.stage + ' · ' + x.calls + ' calls · largest prompt ' + x.max_chars + ' chars') + '">' + esc(x.stage) + ' <b>' + x.calls + '</b></span>').join('') + (r.warnings && r.warnings.length ? '<em title="' + esc(r.warnings.join('\n')) + '">⚠ ' + r.warnings.length + '</em>' : '') + '</div>' : '';
      return '<div class="lp-row"' + itemAttr({ session_id: r.id, goal: r.goal, status: r.status, wall_s: r.wall_s, steps: r.steps, planned: r.planned, calls: r.calls, fails: r.fails, repeats: r.repeats, llm_calls: r.llm_calls, by_tool: r.by_tool, stages: r.stages, warnings: r.warnings }, 'loop ' + r.id) + ' data-ci-ref="' + esc(r.id) + '">'
        + '<span class="lp-g">' + ciState(r.o === 'pass' ? 'green' : r.o === 'running' ? 'racing' : r.o === 'unknown' ? 'none' : 'red') + '<b title="' + esc(r.goal) + '">' + esc(r.goal) + '</b></span>'
        + '<span class="lp-w" title="' + esc(dur(r.wall_s) + ' wall') + '"><i style="width:' + ((r.wall_s || 0) / wmax * 100).toFixed(1) + '%"></i><em>' + dur(r.wall_s) + '</em></span>'
        + '<span class="lp-t" title="' + esc(dur(r.tool_ms / 1000) + ' in tools') + '">' + stack + '</span>'
        + '<span class="lp-n"><b>' + r.calls + '</b>' + (r.fails ? '<em class="bad">' + r.fails + '✗</em>' : '') + (r.repeats ? '<em class="warn">' + r.repeats + '↻</em>' : '') + '<small>' + r.llm_calls + ' llm</small></span>'
        + stages + '</div>';
    }).join('');
    return wrap('loop-perf', head + '<div class="lp-rows' + (sz === 'xl' ? ' scroll' : '') + '"><div class="lp-hd"><span>loop</span><span>wall</span><span>tool time by tool</span><span>calls</span></div>' + body + (rows.length > n ? '<div class="ci-foot">+' + (rows.length - n) + ' more runs</div>' : '') + '</div>', 'ci');
  };
  const big = (sz) => sz === 'l' || sz === 'xl';

  /* THE CENSUS, LIVE (census.live): the run in flight - its goals as a strip (done, the active one, the rest), the
     active goal's time against its wall cap, and where its model calls are going right now (by node, by model,
     tokens per second, the last calls). With nothing in flight it says so and names the last run. */
  R['census-live'] = (d, H, o) => {
    if (!d || typeof d !== 'object') return EMPTY('the live census reads census.live');
    const a = d.active || null, pr = d.progress || {}, h = d.harness || {}, rt = d.routing || {}, sz = (o && o.size) || 'm';
    if (!a && !(h.state === 'running')) return wrap('census-live', '<div class="cl-idle">' + ciRing(null, 34) + '<span><b>No census running</b><small>' + esc(h.census_run ? 'last: ' + h.census_run + ' · ' + (h.goals_done || 0) + '/' + (h.goals_total || '') + ' · ' + (h.state || '') : 'start one from Work · Templates & run') + '</small></span></div>', 'ci');
    const ids = h.goal_ids || [], total = h.goals_total || pr.goals_total || ids.length || 0, done = h.goals_done != null ? h.goals_done : (pr.completed || 0);
    const act = (a && a.goal_id) || pr.active_goal || h.current_goal || '', rem = new Set(pr.remaining || []);
    const strip = (ids.length ? ids : Array.from({ length: total }, (_, i) => 'g' + i)).map((g, i) => { const st = g === act ? 'now' : (rem.has(g) ? 'todo' : (i < done || !rem.size ? 'done' : 'todo')); return '<i class="cl-g ' + st + '" title="' + esc(g + ' · ' + st) + '"></i>'; }).join('');
    const cap = d.wall_cap_s || 1800, el = (a && a.elapsed_s) || 0, fr = Math.min(1, el / cap);
    const bars = (obj, lbl) => { const ks = Object.keys(obj || {}); if (!ks.length) return ''; const mx = Math.max(1, ...ks.map((k) => +obj[k] || 0)); return '<div class="cl-bars"><span class="h">' + lbl + '</span>' + ks.sort((x, y) => obj[y] - obj[x]).slice(0, 5).map((k, i) => '<span class="r"><em title="' + esc(k) + '">' + esc(k) + '</em><i style="width:' + ((+obj[k] || 0) / mx * 100).toFixed(0) + '%;background:' + DV(i) + '"></i><b>' + obj[k] + '</b></span>').join('') + '</div>'; };
    const last = (rt.last || []).slice(-(sz === 'xl' ? 10 : 5)).reverse().map((c) => '<span class="cl-c" title="' + esc([c.ts, c.job, c.role, c.model, c.node, c.status].filter(Boolean).join(' · ')) + '"><b>' + esc(c.role || c.job || '') + '</b><em>' + esc(String(c.model || '').split('/').pop()) + '</em><small>' + (c.tok_s != null ? fmt(c.tok_s) + ' tok/s' : '') + (c.s != null ? ' · ' + fmt(c.s) + 's' : '') + '</small></span>').join('');
    return wrap('census-live', '<div class="ci-hd">' + '<svg class="ci-ring" viewBox="0 0 44 44" width="44" height="44"><circle cx="22" cy="22" r="18" fill="none" stroke="var(--b-s3)" stroke-width="4"/><circle cx="22" cy="22" r="18" fill="none" stroke="' + (fr > .85 ? 'var(--b-ac4)' : fr > .6 ? 'var(--b-ac3)' : 'var(--b-ac)') + '" stroke-width="4" stroke-linecap="round" stroke-dasharray="' + (113.1 * fr).toFixed(1) + ' 113.1" transform="rotate(-90 22 22)"/><text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle">' + Math.round(el / 60) + 'm</text></svg>'
      + '<span class="ci-hdt"><b>' + esc(act || 'census') + ' · goal ' + (done + 1) + ' of ' + total + '</b><small>' + esc([h.census_run, h.template, h.operator ? 'by ' + h.operator : '', Math.round(el / 60) + ' of ' + Math.round(cap / 60) + ' min cap', rt.tok_s_median != null ? fmt(rt.tok_s_median) + ' tok/s median' : ''].filter(Boolean).join(' · ')) + '</small></span></div>'
      + '<div class="cl-strip">' + strip + '</div>'
      + (sz !== 'm' && ids.length ? '<div class="cl-gl">' + ids.map((g, i) => { const st = g === act ? 'now' : (rem.has(g) ? 'todo' : (i < done || !rem.size ? 'done' : 'todo')); return '<span class="' + st + '">' + esc(g) + '</span>'; }).join('') + '</div>' : '')
      + (sz !== 'm' ? '<div class="cl-grid">' + bars(rt.by_node, 'by node') + bars(rt.by_model, 'by model') + '</div>' : '')
      + (last ? '<div class="cl-last">' + last + '</div>' : ''), 'ci');
  };

  /* THE COMMIT GRAPH BESIDE THE CENSUS (ci.census): one timeline, newest at the top - every commit that landed on
     main as a row on its lane (a merged branch gets a lane of its own, its merge a ring), and every census run
     where it ENDED, as a band with its goals done, its change and its verdict. The commits between two bands are
     the ones that run measured: under a green band, a real gain; under a red one, a real loss. */
  R['census-timeline'] = (d, H, o) => {
    const p = (d && d.kind === 'ci' && d.view === 'census') ? d : null; if (!p) return EMPTY('the census timeline reads ci.census');
    const runs = p.runs || []; if (!runs.length) return NODATA('ci.census', 'm');
    const sz = (o && o.size) || 'm';
    const ev = [];
    runs.forEach((r) => { ev.push({ k: 'run', t: Date.parse(r.ended_at) || (+r.ended_at * 1000) || 0, r });
      (r.commits || []).forEach((c) => ev.push({ k: 'c', t: (+c.ts || 0) * 1000, c, run: r.id })); });
    ev.sort((x, y) => y.t - x.t || (x.k === 'run' ? -1 : 1));
    const max = sz === 'xl' ? ev.length : sz === 'l' ? 60 : 24, shown = ev.slice(0, max);
    const lanes = {}; let nl = 1;
    const laneOf = (c) => { const m = /merge (\S+) \(pipeline/i.exec(c.subject || '') || /Merge branch '([^']+)'/.exec(c.subject || ''); const br = c.branch || (m && m[1]) || ''; if (!br) return 0; if (lanes[br] == null) { lanes[br] = nl; nl = nl % 6 + 1; } return lanes[br]; };
    const LW = 12, W0 = 8;
    const rowsH = shown.map((e) => {
      if (e.k === 'run') { const r = e.r, up = r.verdict === 'signal' && r.direction === 'up', dn = r.verdict === 'signal' && r.direction === 'down';
        return '<div class="ct-run' + (up ? ' up' : dn ? ' dn' : '') + '"' + itemAttr({ run: r.id, done: r.done, goals: r.goals, verdict: r.verdict, direction: r.direction, delta_done: r.delta_done, delta_wall: r.delta_wall, commits: (r.commits || []).map((c) => c.sha + ' ' + c.subject) }, 'run ' + r.id) + '>'
          + '<span class="ct-rl"><b>' + esc(r.id) + '</b><small>' + esc(ciWhen(r.ended_at && isFinite(+r.ended_at) ? new Date(+r.ended_at * 1000).toISOString() : r.ended_at)) + '</small></span>'
          + '<span class="ct-db"><i style="width:' + ((r.done || 0) / Math.max(1, r.goals || 1) * 100).toFixed(0) + '%"></i><em>' + r.done + '/' + r.goals + '</em></span>'
          + '<span class="ct-v">' + (r.delta_done != null ? (r.delta_done > 0 ? '+' : '') + r.delta_done + ' · ' + (r.delta_wall > 0 ? '+' : '') + Math.round((r.delta_wall || 0) * 100) + '% wall' : 'first') + ' <b class="' + (up ? 'ok' : dn ? 'bad' : '') + '">' + (r.verdict === 'signal' ? (up ? '▲ real gain' : dn ? '▼ real loss' : 'signal') : r.verdict === 'noise' ? 'within noise' : '') + '</b> · ' + r.commit_count + ' commits</span></div>'; }
      const c = e.c, ln = laneOf(c), x = W0 + ln * LW;
      return '<div class="ct-c' + (c.merge ? ' mg' : '') + '" title="' + esc(c.sha + ' · ' + c.subject + (c.during_run ? ' · landed DURING the run' : '')) + '"' + itemAttr({ sha: c.sha, subject: c.subject, branch: c.branch, pipeline: c.pipeline, run: e.run }, 'commit ' + c.sha) + '>'
        + '<svg class="ct-g" width="' + (W0 * 2 + 6 * LW) + '" height="20"><line x1="' + W0 + '" y1="0" x2="' + W0 + '" y2="20" stroke="var(--b-t3)" stroke-width="2"/>' + (ln ? '<path d="M' + x + ' 0 C' + x + ' 10 ' + W0 + ' 10 ' + W0 + ' 20" fill="none" stroke="' + DV(ln) + '" stroke-width="1.6"/>' : '')
        + '<circle cx="' + (ln ? x : W0) + '" cy="10" r="' + (c.merge ? 4.5 : 3.5) + '" fill="' + (c.merge ? 'var(--b-surf)' : (ln ? DV(ln) : 'var(--b-t2)')) + '" stroke="' + (ln ? DV(ln) : 'var(--b-t2)') + '" stroke-width="' + (c.merge ? 2 : 0) + '"/></svg>'
        + '<code>' + esc(c.sha) + '</code><span class="ct-s">' + esc(c.subject) + '</span>' + (c.during_run ? '<em title="landed while the run was going - the run measured some of its goals before it and some after">◐</em>' : '') + '</div>';
    }).join('');
    return wrap('census-timeline', '<div class="ci-hd"><span class="ci-hdt"><b>' + esc(p.title || 'Census') + ' timeline · ' + esc(p.template || '') + '</b><small>newest first · a band is a census run, where it ended · the commits under it landed before it · ' + ev.length + ' rows' + (ev.length > shown.length ? ', newest ' + shown.length + ' drawn' : '') + '</small></span></div><div class="ct-rows' + (sz === 'xl' ? ' scroll' : '') + '">' + rowsH + '</div>', 'ci');
  };
  Object.assign(DRAWN, { 'loop-perf': 'items', 'census-live': 'stages', 'census-timeline': 'events' });
  Object.assign(CI_GLYPH, { 'loop-perf': '⏱', 'census-live': '◔', 'census-timeline': '⎇' });
  Object.assign(FORM_SAMPLE, {
    'loop-perf': () => ({ kind: 'ci', view: 'loop-perf', rows: [['Make the timer better.', 'cancelled', 1805, 45, 69, [['exec.bash.run', 608647], ['memory.seek', 28866]]], ['Write a 200-word explainer', 'done', 1009, 13, 24, [['prose.author', 128061], ['code.edit', 53987]]], ['Generate 200 random integers', 'done', 471, 7, 10, [['code.author', 40029], ['exec.python.run', 10860]]], ['What is 17 multiplied by 23?', 'done', 128, 2, 8, [['memory.seek', 12956]]]].map((x, i) => ({ id: 's' + i, goal: x[0], status: x[1], o: x[1] === 'done' ? 'pass' : 'fail', started_at: '2026-09-27T2' + i + ':00:00Z', wall_s: x[2], steps: 2, planned: 2, calls: x[3], fails: i === 0 ? 3 : 0, repeats: i === 0 ? 5 : 0, llm_calls: x[4], tool_ms: x[5].reduce((a, t) => a + t[1], 0), by_tool: x[5].map((t) => ({ tool: t[0], ms: t[1] })), stages: [{ stage: 'planner', calls: 2 }, { stage: 'controller', calls: 4 }] })),
      summary: { runs: 16, pass_rate: 0.93, wall_median: 499, wall_p90: 1620, calls_median: 4.5, llm_median: 10, tools: [{ tool: 'exec.bash.run', ms: 636151 }, { tool: 'operator.run', ms: 384968 }, { tool: 'code.author', ms: 345710 }, { tool: 'prose.author', ms: 309730 }] } }),
    'census-live': () => ({ active: { goal_id: 'long-horizon', elapsed_s: 928 }, progress: { goals_total: 12, completed: 7, active_goal: 'long-horizon', remaining: ['analyse-data', 'research-web', 'operate-exec', 'prose-only'] }, wall_cap_s: 1800,
      harness: { census_run: 'default-style-detailed-run1', template: 'default', operator: 'claude', goals_total: 12, goals_done: 7, state: 'running', goal_ids: ['build-simple-code', 'author-then-edit', 'build-browser-verified', 'build-multifile', 'trivial-chat', 'underspecified', 'research-report', 'long-horizon', 'analyse-data', 'research-web', 'operate-exec', 'prose-only'] },
      routing: { tok_s_median: 23.5, by_node: { 'gpu-250': 14, 'cpu-246': 6, 'cpu-247': 5 }, by_model: { 'qwen3.5:9b': 9, 'nomic-embed-text': 8, 'qwen2.5:7b': 8 }, last: [{ role: 'planner', model: 'qwen3.5:9b', node: 'gpu-250', tok_s: 24.1, s: 12.4 }, { role: 'embed', model: 'nomic-embed-text', node: 'cpu-246', s: 0.2 }, { role: 'controller', model: 'qwen3.5:9b', node: 'gpu-250', tok_s: 22.8, s: 31.0 }] } }),
    'census-timeline': () => FORM_SAMPLE['census-commits'](),
  });
  const PF_CSS = '.vb-loop-perf,.vb-census-live,.vb-census-timeline{display:flex;flex-direction:column;gap:7px;min-height:0;height:100%}'
    + '.lp-kpis{display:flex;align-items:center;gap:14px;flex-wrap:wrap;flex:none}.lp-kpis>span{display:flex;flex-direction:column}.lp-kpis b{font:600 14px/1.1 var(--b-mono);color:var(--b-t1)}.lp-kpis b.warn{color:var(--b-ac3)}.lp-kpis small{font-size:8.5px;color:var(--b-t3);text-transform:uppercase;letter-spacing:.06em}'
    + '.lp-leg{display:flex!important;flex-direction:row!important;flex-wrap:wrap;gap:3px 10px;margin-left:auto;font-size:9.5px;color:var(--b-t2);align-items:center}.lp-leg i{width:8px;height:8px;border-radius:2px;background:var(--c);display:inline-block;margin-right:3px}.lp-leg em{font-style:normal;color:var(--b-t3);margin-right:4px}'
    + '.lp-rows{display:flex;flex-direction:column;gap:2px;min-height:0;flex:1}.lp-hd,.lp-row{display:grid;grid-template-columns:minmax(120px,34%) minmax(70px,18%) minmax(0,1fr) 92px;gap:8px;align-items:center}.lp-hd{font:8.5px var(--b-ui);text-transform:uppercase;letter-spacing:.07em;color:var(--b-t3);padding:0 4px}'
    + '.lp-row{padding:3px 4px;border-radius:5px;cursor:pointer}.lp-row:hover{background:var(--b-surf2)}.lp-g{display:flex;align-items:center;gap:6px;min-width:0}.lp-g b{font-size:10.5px;font-weight:500;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.lp-w{position:relative;height:14px;background:var(--b-surf2);border-radius:3px;overflow:hidden}.lp-w i{position:absolute;left:0;top:0;bottom:0;background:' + mix(B.ac, 45) + '}.lp-w em{position:absolute;left:5px;top:0;font:9px/14px var(--b-mono);color:var(--b-t1);font-style:normal}'
    + '.lp-t{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--b-surf2)}.lp-t i{display:block;height:100%;min-width:2px}.lp-n{display:flex;align-items:baseline;gap:4px;font:600 10.5px var(--b-mono);color:var(--b-t1);white-space:nowrap}.lp-n em{font-style:normal;font-size:9px}.lp-n em.bad{color:var(--b-ac4)}.lp-n em.warn{color:var(--b-ac3)}.lp-n small{font-weight:400;font-size:9px;color:var(--b-t3)}'
    + '.lp-st{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:4px 10px;padding-left:22px;font-size:9px;color:var(--b-t3)}.lp-st b{color:var(--b-t2);font-family:var(--b-mono)}.lp-st em{font-style:normal;color:var(--b-ac3)}'
    + '.cl-idle{display:flex;align-items:center;gap:10px;height:100%}.cl-idle span{display:flex;flex-direction:column}.cl-idle b{font-size:12px}.cl-idle small{font-size:10px;color:var(--b-t3)}'
    + '.cl-strip{display:flex;gap:3px;flex:none}.cl-g{flex:1;height:10px;border-radius:3px;background:var(--b-surf2)}.cl-g.done{background:var(--b-ac2)}.cl-g.now{background:repeating-linear-gradient(90deg,var(--b-ac) 0 6px,' + mix(B.ac, 45) + ' 6px 12px);background-size:12px 100%;animation:ciRun .8s linear infinite}'
    + '.cl-gl{display:flex;flex-wrap:wrap;gap:3px}.cl-gl span{font:9px var(--b-mono);padding:2px 6px;border-radius:4px;background:var(--b-surf2);color:var(--b-t3)}.cl-gl span.done{color:var(--b-ac2)}.cl-gl span.now{color:var(--b-on,#111);background:var(--b-ac)}'
    + '.cl-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.cl-bars{display:flex;flex-direction:column;gap:3px}.cl-bars .h{font:8.5px var(--b-ui);text-transform:uppercase;letter-spacing:.07em;color:var(--b-t3)}.cl-bars .r{display:grid;grid-template-columns:minmax(0,40%) 1fr 26px;gap:6px;align-items:center}.cl-bars em{font:9.5px var(--b-mono);font-style:normal;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cl-bars i{height:8px;border-radius:2px}.cl-bars b{font:9px var(--b-mono);color:var(--b-t3);text-align:right}'
    + '.cl-last{display:flex;gap:5px;flex-wrap:wrap}.cl-c{display:flex;flex-direction:column;padding:4px 7px;border-radius:6px;background:var(--b-surf2);min-width:0}.cl-c b{font-size:9.5px;color:var(--b-t1)}.cl-c em{font:9px var(--b-mono);font-style:normal;color:var(--b-t2)}.cl-c small{font-size:8.5px;color:var(--b-t3)}'
    + '.ct-rows{display:flex;flex-direction:column;min-height:0;flex:1}.ct-c{display:flex;align-items:center;gap:6px;height:20px;cursor:pointer;min-width:0;border-radius:3px}.ct-c:hover{background:var(--b-surf2)}.ct-g{flex:none}.ct-c code{font:9px var(--b-mono);color:var(--b-t3);flex:none}.ct-s{font-size:10px;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}.ct-c.mg .ct-s{color:var(--b-t2)}.ct-c em{font-style:normal;font-size:8.5px;color:var(--b-ac3);flex:none}'
    + '.ct-run{display:grid;grid-template-columns:minmax(120px,22%) minmax(80px,20%) minmax(0,1fr);gap:10px;align-items:center;padding:5px 8px;margin:3px 0;border-radius:6px;background:var(--b-surf2);border-left:3px solid var(--b-t3);cursor:pointer}.ct-run.up{border-left-color:var(--b-ac2);background:' + mix(B.ac2, 12) + '}.ct-run.dn{border-left-color:var(--b-ac4);background:' + mix(B.ac4, 12) + '}'
    + '.ct-rl{display:flex;flex-direction:column;min-width:0}.ct-rl b{font:600 10.5px var(--b-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ct-rl small{font-size:8.5px;color:var(--b-t3)}.ct-db{position:relative;height:14px;background:var(--b-surf);border-radius:3px;overflow:hidden}.ct-db i{position:absolute;left:0;top:0;bottom:0;background:var(--b-ac2)}.ct-db em{position:absolute;left:5px;font:9px/14px var(--b-mono);font-style:normal;color:var(--b-t1)}.ct-v{font-size:9.5px;color:var(--b-t2)}';

  const FORMS3_CSS = '.vb-calh{display:flex;align-items:center;gap:6px;flex:none}.vb-calh b{font-size:12px;font-weight:600;flex:1;text-align:center}.vb-calh button{width:22px;height:20px;border-radius:5px;color:var(--b-t2);box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-calh button:hover{color:var(--b-t1);box-shadow:inset 0 0 0 1px var(--b-ac)}.vb-calh button.today{width:auto;padding:0 8px;font-size:10px}.vb-calh.big b{font-size:14px}'
    + '.vb-mgrid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px;flex:1;min-height:0;align-content:start}.vb-mgrid .wd{font-size:9px;color:var(--b-t3);text-align:center;text-transform:uppercase;letter-spacing:.05em}'
    + '.vb-mday{position:relative;border-radius:4px;background:color-mix(in srgb,var(--b-t1) 5%,transparent);padding:2px 3px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column;gap:1px;min-width:0}.vb-mday .n{font-size:10px;color:var(--b-t2);font-family:var(--b-mono)}.vb-mday.out{opacity:.45}.vb-mday.today{box-shadow:inset 0 0 0 1.5px var(--b-ac)}.vb-mday.today .n{color:var(--b-ac);font-weight:700}.vb-mday.sel{background:color-mix(in srgb,var(--b-ac) 22%,transparent)}.vb-mday:hover{background:color-mix(in srgb,var(--b-t1) 10%,transparent)}'
    + '.vb-mday .ev{display:block;font-style:normal;font-size:9.5px;line-height:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:4px;border-left:2px solid var(--c);color:var(--b-t1);border-radius:1px}.vb-mday .ev:hover{background:color-mix(in srgb,var(--c) 25%,transparent)}.vb-mday .more{font-style:normal;font-size:9px;color:var(--b-t3)}.vb-mday .dot{width:5px;height:5px;border-radius:50%;margin:0 auto}'
    + '.vb-sdh{display:flex;align-items:baseline;gap:6px;font-size:10.5px;font-weight:600;color:var(--b-t2);text-transform:uppercase;letter-spacing:.05em;padding:4px 0 2px;border-bottom:1px solid var(--b-bd);cursor:pointer}.vb-sdh.today{color:var(--b-ac)}.vb-sdh small{margin-left:auto;font-weight:400;color:var(--b-t3)}'
    + '.vb-sde{display:grid;grid-template-columns:4.4em minmax(0,1fr);gap:8px;align-items:baseline;padding:3px 0 3px 8px;border-left:3px solid var(--c);cursor:pointer;border-radius:2px}.vb-sde:hover{background:var(--b-s2)}.vb-sde .t{font-family:var(--b-mono);font-size:10px;color:var(--b-t2);white-space:nowrap}.vb-sde .t small{font-size:9px}.vb-sde .x{min-width:0}.vb-sde b{display:block;font-weight:500;font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-sde small{display:block;font-size:10px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.vb-calv{display:flex;gap:3px;justify-content:center}.vb-calv button{padding:2px 10px;border-radius:999px;font-size:10.5px;color:var(--b-t2);box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-calv button.on{background:var(--b-ac);color:var(--b-on);box-shadow:none}'
    + '.vb-vgraph{position:relative;width:100%;flex:1;min-height:80px;border-radius:var(--b-r);overflow:hidden}.vb-vgraph > slot{display:block;width:100%;height:100%}'
    + '[data-item]{cursor:pointer}';

  /* ── draw at a size: the composition around the form ─────────────────── */
  const GLYPH = { context_graph: '◎', trace: '∿', radial: '◔', counter: '123', bar: '▬', bars: '▥', thermo: '≣', heat: '▦', matrix: '▦', donut: '◑', stack: '▤', pills: '◦', log: '≡', lane: '≡', table: '▦', files: '⊞', list: '≡', checklist: '☑', stepper: '⋮', calendar: '▦', string: '¶', kv: '≔', pipes: '⌥', scatter: '⁘', panel: '▭', composite: '⊞', lines: '≋' };
  // the glyph a size below M carries (the Sizes board): a ring for a level or a share, a spark for a series, a tube for
  // named values, a dot for events and graphs, the count glyph for the rest — drawn from the data, never a character
  function glyphOf(form, data) {
    const f = canon(form), sh = DRAWN[f] || '', d = dataFor(data, f); const ringG = (fr, col) => '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="var(--b-s3,#222630)" stroke-width="3"/><circle cx="8" cy="8" r="6" fill="none" stroke="' + col + '" stroke-width="3" stroke-dasharray="' + (2 * Math.PI * 6 * Math.max(0, Math.min(1, fr))).toFixed(1) + ' 37.7" transform="rotate(-90 8 8)"/></svg>';
    if (CI_FORMS.test(f)) { const p = ciOf(d, f), pr = p && p.summary && p.summary.pass_rate; return pr != null ? ringG(pr, pr >= .9 ? 'var(--b-ac2,#5ec9a0)' : pr >= .6 ? 'var(--b-ac3,#e09a55)' : 'var(--b-ac4,#e06060)') : (CI_GLYPH[f] || '▦'); }
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
    const proj = String(opts.projection || (opts.record && opts.record.projection) || (opts.draw && opts.draw.proj) || '').toLowerCase();
    const fi = proj === 'iso' ? (R[f0.toLowerCase() + '@iso'] ? f0.toLowerCase() + '@iso' : (R[f + '@iso'] ? f + '@iso' : f)) : f;
    if (opts.map !== false) { const rec0 = opts.record; const m = opts.map || (rec0 && rec0.read && rec0.read.map), rg = opts.range || (rec0 && rec0.read && rec0.read.range), du = opts.draw && opts.draw.unit;
      if ((m && typeof m === 'object' && Object.keys(m).length) || (Array.isArray(rg) && rg.length === 2) || du) data = mapped({ read: { map: opts.map || m, range: opts.range || rg }, draw: { unit: du } }, f, data); }
    const d = dataFor(data, f);
    // nothing to draw yet (no result, an empty one, or a placeholder string handed to a form that draws numbers): the
    // form's SAMPLE face, marked — never "no data yet" (opts.sample === false keeps the bare answer for a caller that asks)
    if (f !== 'panel' && f !== 'composite' && f !== 'calnav' && f !== 'element' && (isEmpty(d) || (typeof d === 'string' && DRAWN[f] !== 'string'))) {
      if (opts.sample === false) { let own = ''; try { own = R[fi](d == null || typeof d === 'string' ? [] : d, H, Object.assign({ size: size }, opts)); } catch (_) { own = ''; } return (typeof own === 'string' && own) ? own : EMPTY('no data yet'); }
      return sampleFace(draw(f0, sample(f), size, Object.assign({}, opts, { sample: false, map: false })), size, opts.sampleTag);
    }
    if (f === 'composite' && !(opts.record && Array.isArray(opts.record.children) && opts.record.children.length)) {
      if (opts.sample === false) return EMPTY('a composite needs children');
      return sampleFace(draw(f0, data, size, Object.assign({}, opts, { sample: false, record: Object.assign({}, opts.record || {}, sample('composite')) })), size, opts.sampleTag);
    }
    if (size === 'xs') return '<span class="vw-xs" title="' + esc(opts.title || f0) + '"><i class="vw-g">' + glyphOf(f, data) + '</i>' + (figure(f, data) || '—') + '</span>';
    if (size === 's' && !CI_FORMS.test(f) && (DRAWN[f] === 'items' || f === 'kv' || f === 'pills' || f === 'temps' || f === 'numbers')) { const cs = chipRow(f, d, opts); if (cs) return cs; }
    if (size === 's' && f !== 'calnav') return '<span class="vw-chip" title="' + esc(opts.title || f0) + '"><i class="vw-g">' + glyphOf(f, data) + '</i><b>' + (figure(f, data) || '—') + '</b>' + (opts.title ? '<small>' + esc(opts.title) + '</small>' : '') + '</span>';
    let body; try { body = R[fi](d, H, Object.assign({ size: size }, opts)); } catch (e) { body = EMPTY('could not draw ' + f0 + ': ' + (e && e.message || e)); }
    if (size === 'm' || opts.bare || TABLE_FORMS.has(f) || CI_FORMS.test(f) || f === 'composite' || DRAWN[f] === 'events' || /^(json|diff|code|progress|status|media|error|markdown|terminal|string|kv|numbers|month|schedule|calnav|vgraph)$/.test(f)) return body;   // a result form is its own composition: it takes the whole body   // a composite, a table, a feed: the body is the composition
    // L: the form plus its detail list beside it; XL: the form, its table, its log
    // the detail list beside the form holds the rows its body has room for (~16 px a row) - eight in a two-row tile ran
    // past its foot - and a form that already names every value it draws (ranked bars, pills, a number grid, a
    // legend) has none: the list only repeated it
    const dRows = Math.max(2, Math.min(8, Math.floor(((opts.height || HEIGHT[size]) - 4) / 16)));
    const LABELLED = /^(ranked|bullet|lollipop|temps|pills|numbers|kv|funnel|stacked-bar|treemap|donut|waffle|gauge|threshold|diverging|radar|pareto|histogram|column|bars|heat|matrix|small-multiples|spark-table|horizon|lines)$/;
    const kv = LABELLED.test(f) ? [] : keyed(d).slice(0, dRows); const rw = LABELLED.test(f) ? [] : rows(d).slice(0, dRows);
    const detail = kv.length ? '<div class="vw-detail">' + kv.map((x) => '<div><span>' + esc(x[0]) + '</span><b>' + esc(fmt(x[1])) + '</b></div>').join('') + '</div>'
      : (rw.length ? '<div class="vw-detail">' + rw.slice(0, 8).map((r) => '<div><span>' + esc(String(r.name ?? r.title ?? r.text ?? r.path ?? r.id ?? '')) + '</span><b>' + esc(String(r.value ?? r.v ?? r.status ?? r.count ?? '')) + '</b></div>').join('') + '</div>'
      : (Array.isArray(d) && d.length ? '<div class="vw-detail"><div><span>points</span><b>' + d.length + '</b></div><div><span>last</span><b>' + esc(fmt(series(d).slice(-1)[0])) + '</b></div><div><span>min · max</span><b>' + esc(fmt(Math.min(...series(d)))) + ' · ' + esc(fmt(Math.max(...series(d)))) + '</b></div></div>' : ''));
    // XL adds its table under the form only when the body has the room for one (a wide, three-row tile drew the table below
    // the fold and cut it); a shorter XL body composes as L - the form and its detail beside it
    if (size === 'l' || (opts.height && opts.height < 220)) return '<div class="vw-l"><div class="vw-main">' + body + '</div>' + detail + '</div>';
    const table = (kv.length || rw.length) && !TABLE_FORMS.has(f) ? R.table(rw.length ? rw : kv.map((x) => ({ name: x[0], value: x[1] })), 140, { size: 'l' }) : '';
    return '<div class="vw-xl"><div class="vw-main">' + body + '</div>' + detail + (table ? '<div class="vw-xltable">' + table + '</div>' : '') + '</div>';
  }
  /* DERIVED forms: drawn like any other, but their DATA comes from a capability and cannot be written by hand — a
     structured graph is the Explode contract. They are marked here so the lists people and models pick from can
     leave them out; offered as a menu choice, one gets picked for an ordinary chart and draws its empty state. */
  const DERIVED = new Set(['structgraph']);
  function forms() { return Object.keys(DRAWN).map((id) => ({ id, shape: DRAWN[id], sizes: SIZES.slice(), drawn: true, derived: DERIVED.has(id), iso: !!R[id + '@iso'] || ISO_ONLY.has(id) })).concat(Object.keys(ALIAS).filter((id) => !DRAWN[id]).map((id) => ({ id, shape: DRAWN[ALIAS[id]] || '', sizes: SIZES.slice(), drawn: true, as: ALIAS[id] }))); }
  const ISO_ONLY = new Set(['dial', 'tank', 'stacks', 'conveyor', 'city', 'shelf', 'stack', 'sweep', 'meter-panel', 'pipes', 'library', 'pages', 'approvals', 'wiki', 'notices', 'devices', 'notebook', 'hosts', 'containers', 'models', 'datasets', 'sandboxes', 'activity', 'frame', 'racks']);

  /* ══ THE IMAGE STUDIO'S FORMS (owner, 2026-09-27: "can we have a widget for displaying sprites and characters and images
     from the image studio") ═══════════════════════════════════════════════════════════════════════════════════════════
       images     a justified grid of real thumbnails: each tile as wide as its image's aspect (width · height from the row,
                  else square), lazy-loaded, its prompt or title on hover; a row that is not an image (an html report) is a
                  tile of its kind. Every tile carries its row, so a click opens the drawer - with the image large.
       sprite     one sprite sheet animated: its frames stepped at the sheet's own fps (CSS steps, no script), pixel-crisp on
                  a checkerboard; it plays by itself and holds still under the pointer (draw.play 'hover' plays it only
                  there); the strip under it is its animations - a click shows that one. It reads a spritegen record
                  (sheets + urls), a character's sheet, a list of either (draw.id picks one, else the first with a sheet)
                  or a bare sheet {url, columns, rows, count, fps, frame_width, frame_height}; frames with no sheet flip.
       sprites    the sprite library: a card per sprite character, its idle (else first) animation playing under the
                  pointer, its name, what it has (animations · size); the ones with nothing drawn yet are counted
       character  a character card: the portrait, the name and what it is, its traits as chips, its numbers (stats) as
                  bars, its expressions and its sprites as strips; a list draws the one draw.id names (else the first) - or
                  with draw.roster every one as a small card
     Motion is off when the record says frame.motion false (and under prefers-reduced-motion): a sprite shows its first
     frame. Addresses the studio answers with ('/images/file/…', '/spritegen/asset?…') are read against the host's base. */
  const stAbs = (u, o) => { const s = String(u == null ? '' : u).trim(); if (!s) return ''; if (/^(data:|https?:|blob:)/i.test(s)) return s; const b = String((o && o.base) || '').replace(/\/$/, ''); return s.charAt(0) === '/' ? b + s : s; };
  const cssUrl = (u) => String(u).replace(/["\\\n\r]/g, (c) => encodeURIComponent(c));
  const IMG_RE = /\.(png|jpe?g|gif|webp|avif|svg|bmp)(\?|#|$)|^data:image\/|\/images\/file\/|\/(spritegen|character)\/asset\?/i;
  const stStill = (o) => !!((o && o.record && o.record.frame && o.record.frame.motion === false) || (o && o.draw && o.draw.motion === false));
  // the picture a row is: a thumbnail first, else an address that is an image, else a studio record's own portrait
  function imgOf(r) {
    if (!r) return ''; if (typeof r === 'string') return IMG_RE.test(r) ? r : '';
    if (typeof r !== 'object' || Array.isArray(r)) return '';
    const b = r.image_b64 || r.b64; if (typeof b === 'string' && b.length > 32) { const m = mediaOf({ image_b64: b }); if (m) return m.url; }
    for (const k of ['thumb', 'thumbnail', 'thumb_url', 'preview', 'portrait', 'image_url', 'image', 'src', 'url']) { const v = r[k];
      if (typeof v !== 'string' || !v) continue;
      if (IMG_RE.test(v) || (/^(thumb|thumbnail|thumb_url|preview|portrait)$/.test(k) && /^(\/|https?:)/.test(v)) || ((k === 'url' || k === 'src') && /image/i.test(String(r.kind ?? r.mime ?? r.content_type ?? '')))) return v; }
    const u = r.urls; if (u && typeof u === 'object' && typeof u.reference === 'string' && u.reference) return u.reference;
    const f = r.frame_urls; if (f && typeof f === 'object') { const v = f.neutral || f[Object.keys(f)[0]]; if (typeof v === 'string' && v) return v; }
    return '';
  }
  // every animation a record carries, one shape: {name, url | frames, cols, rows, n, fps, fw, fh, loop}
  function spriteAnims(r) {
    const out = []; if (!r || typeof r !== 'object' || Array.isArray(r)) return out;
    const push = (name, g, url, frames) => { if (out.some((x) => x.name === String(name))) return; g = g || {};
      const fl = Array.isArray(frames) ? frames.filter((x) => typeof x === 'string' && x) : null;
      const n = Math.max(1, Math.round(num(g.count ?? g.frame_count ?? (typeof g.frames === 'number' ? g.frames : 0)) || (fl ? fl.length : 1)));
      const cols = Math.max(1, Math.round(num(g.columns ?? g.cols) || (fl ? 1 : n))), rws = Math.max(1, Math.round(num(g.rows) || Math.ceil(n / cols)));
      out.push({ name: String(name), url: url || '', frames: url ? null : fl, n: fl && !url ? fl.length : n, cols, rows: rws, fps: Math.max(1, Math.min(60, num(g.fps) || 8)), fw: num(g.frame_width ?? g.fw), fh: num(g.frame_height ?? g.fh), loop: g.loop !== false }); };
    // a spritegen record: its sheets (geometry) and their addresses, its animations' frames where no sheet was built
    const sh = (r.sheets && typeof r.sheets === 'object' && !Array.isArray(r.sheets)) ? r.sheets : {}, us = (r.urls && r.urls.sheets) || {}, uf = (r.urls && r.urls.frames) || {};
    const an = (r.animations && typeof r.animations === 'object' && !Array.isArray(r.animations)) ? r.animations : {};
    [...new Set(Object.keys(an).concat(Object.keys(sh), Object.keys(uf)))].forEach((k) => { const g = Object.assign({}, an[k] || {}, sh[k] || {});
      const u = (us[k] && (us[k].png || us[k].url)) || (sh[k] && typeof sh[k].url === 'string' ? sh[k].url : ''); const fr = Array.isArray(uf[k]) && uf[k].length ? uf[k] : null;
      if (u || fr) push(k, g, u, fr); });
    // a companion character's sheet (character.list: sheet.animations, each with its address)
    const cs = (r.sheet && typeof r.sheet === 'object') ? r.sheet : null;
    if (cs) { const A = (cs.animations && typeof cs.animations === 'object') ? cs.animations : null;
      if (A) Object.keys(A).forEach((k) => { if (A[k] && typeof A[k].url === 'string' && A[k].url) push(k, A[k], A[k].url, null); });
      else if (typeof cs.url === 'string' && cs.url) push(cs.anim || 'sheet', cs, cs.url, null); }
    // a bare sheet, or a bare list of frames
    if (!out.length) { const u = typeof r.sheet === 'string' ? r.sheet : (((typeof r.url === 'string' && r.url) || (typeof r.src === 'string' && r.src)) && (r.columns || r.count || r.frame_width) ? (r.url || r.src) : '');
      if (u) push(r.anim || r.animation || 'sheet', r, u, null);
      else if (Array.isArray(r.frames) && r.frames.length && typeof r.frames[0] === 'string' && IMG_RE.test(r.frames[0])) push(r.anim || r.animation || 'frames', r, '', r.frames); }
    return out;
  }
  // the keyframes that step a sheet: one per frame, held (step-end) until the next - named by the grid, so one set serves every sheet of that grid
  const kfName = (a) => 'vwsp-' + a.cols + 'x' + a.rows + 'n' + a.n;
  function kfCss(a) {
    const pos = (k) => { const c = k % a.cols, rr = Math.floor(k / a.cols); return (a.cols > 1 ? c / (a.cols - 1) * 100 : 0).toFixed(3) + '% ' + (a.rows > 1 ? rr / (a.rows - 1) * 100 : 0).toFixed(3) + '%'; };
    let s = '@keyframes ' + kfName(a) + '{'; for (let k = 0; k < a.n; k++) s += (k / a.n * 100).toFixed(3) + '%{background-position:' + pos(k) + '}';
    return s + '100%{background-position:' + pos(a.n - 1) + '}}';
  }
  // one animation at a height (no wider than maxW): the sheet as a background stepped through its frames, or its frames
  // stacked and flipped; its keyframes ride with the markup, so any host that draws it animates it
  function spriteBox(a, h, o, play, cls, maxW) {
    let ar = (a.fw > 0 && a.fh > 0) ? a.fw / a.fh : 1; h = Math.max(8, Math.round(h)); let w = Math.round(h * ar);
    if (maxW && w > maxW) { w = Math.round(maxW); h = Math.max(8, Math.round(w / ar)); }
    const mv = !stStill(o) && a.n > 1, dur = a.n / a.fps, box = '<span class="vw-spr' + (cls ? ' ' + cls : '') + '" data-play="' + (play === 'hover' ? 'hover' : 'auto') + '" style="width:' + w + 'px;height:' + h + 'px">';
    if (a.url) return box + (mv ? '<style>' + kfCss(a) + '</style>' : '') + '<i style="background-image:url(&quot;' + esc(cssUrl(stAbs(a.url, o))) + '&quot;);background-size:' + (a.cols * 100) + '% ' + (a.rows * 100) + '%' + (mv ? ';animation:' + kfName(a) + ' ' + dur.toFixed(3) + 's step-end infinite' : '') + '"></i></span>';
    const fr = (a.frames || []).slice(0, 64), n = fr.length;
    return box + (mv ? '<style>@keyframes vwfl-' + n + '{0%{opacity:1}' + (100 / n).toFixed(3) + '%{opacity:0}100%{opacity:0}}</style>' : '')
      + fr.map((f, i) => '<img src="' + esc(stAbs(f, o)) + '" alt="" loading="lazy" decoding="async"' + (i ? ' class="nx"' : '') + (mv ? ' style="animation:vwfl-' + n + ' ' + dur.toFixed(3) + 's step-end infinite;animation-delay:' + (-(n - i) / n * dur).toFixed(3) + 's"' : '') + '>').join('') + '</span>';
  }
  // the record a single-thing form draws out of what it was handed: the record itself, or from a list (an envelope's one
  // list of records) the one draw.id names, else the first that has something to draw
  function pickRec(d, o, has) {
    let L = Array.isArray(d) ? d : null;
    if (!L && d && typeof d === 'object') { const ks = Object.keys(d).filter((k) => Array.isArray(d[k]) && d[k].some((x) => x && typeof x === 'object' && !Array.isArray(x))); if (ks.length === 1) L = d[ks[0]]; else return d; }
    if (!L) return null; const rw = L.filter((x) => x && typeof x === 'object' && !Array.isArray(x)); const id = String((o && o.draw && (o.draw.id ?? o.draw.pick)) ?? '');
    return (id && rw.find((x) => [x.char_id, x.agent_id, x.id, x.name, x.display_name, x.label].some((v) => v != null && String(v) === id))) || rw.find(has || (() => true)) || rw[0] || null;
  }
  // an envelope that answered with nothing ({characters: [], count: 0}): every list in it empty and nothing else but figures -
  // the form says no data, as the element does for a list form (the fallback to a key . value list drew 'count 0')
  const emptyEnv = (d) => (Array.isArray(d) && !d.some((x) => x && typeof x === 'object')) || (!!d && typeof d === 'object' && !Array.isArray(d) && Object.keys(d).some((k) => Array.isArray(d[k])) && Object.keys(d).every((k) => (Array.isArray(d[k]) ? !d[k].length : (d[k] == null || typeof d[k] !== 'object'))));
  const noneRead = (o) => NODATA((o && o.record && o.record.source) || '', (o && o.size) || 'm');
  const recName = (r) => String(r.display_name || r.name || r.label || r.title || r.char_id || r.agent_id || r.id || '');
  // what a click on a sprite hands the drawer: the sprite in plain fields (the drawer draws the sheet from them, animated)
  const spriteItem = (r, a) => { const A = spriteAnims(r); return { name: recName(r), id: String(r.char_id || r.agent_id || r.id || ''), animation: a.name, frames: a.n, fps: a.fps,
    frame: a.fw && a.fh ? a.fw + '×' + a.fh : '', sheet: a.url || '', sheet_grid: a.url ? a.cols + '×' + a.rows : '', animations: A.map((x) => x.name).join(', '), brief: String(r.brief || r.description || ''),
    style: r.style || '', sprite_size: r.sprite_size, preview: imgOf(r) || '', updated_at: r.updated_at || r.created_at || '' }; };
  const charItem = (r) => { const A = spriteAnims(r); return { name: recName(r), id: String(r.agent_id || r.char_id || r.id || ''), description: String(r.description || r.brief || ''), style: r.style || '', render_mode: r.render_mode || '',
    voice: r.voice || '', states: Array.isArray(r.states) ? r.states.join(', ') : '', animations: A.map((x) => x.name).join(', '), preview: imgOf(r) || '', prompt: String(r.base_prompt || '').slice(0, 400), updated_at: r.updated_at || r.created_at || '' }; };
  R.images = (d, H, o) => {
    const all = rows(d), str = (Array.isArray(d) ? d : []).filter((x) => typeof x === 'string'); const items = all.length ? all : str.map((s) => ({ url: s, name: s.split(/[/?]/).filter(Boolean).pop() || s }));
    if (!items.length) return EMPTY('images need rows with an image address');
    const size = (o && o.size) || 'm', th = Math.max(28, num(o && o.draw && o.draw.thumb) || ({ m: 64, l: 84, xl: 112 }[size] || 64)), gap = 4;
    const body = Math.max(th, (H || HEIGHT[size] || 96) - 4), W = Math.max(th, (o && o.width) || ({ m: 300, l: 460, xl: 720 }[size] || 300));
    const arOf = (r) => { const w = num(r.width ?? r.w), h = num(r.height ?? r.h); return w > 0 && h > 0 ? Math.max(0.4, Math.min(2.5, w / h)) : 1; };
    // as many as the rows the body holds take (a row's count is its images' widths at the tile height); XL scrolls to 60;
    // draw.limit says otherwise
    let lim = Math.round(num(o && o.draw && o.draw.limit));
    if (!lim) { const nRows = Math.max(1, Math.floor((body + gap) / (th + gap))); let acc = 0, row = 0, i = 0;
      for (; i < items.length; i++) { const tw = th * arOf(items[i]) + gap; if (acc > 0 && acc + tw > W + gap) { row++; acc = 0; } if (row >= nRows) break; acc += tw; }
      lim = size === 'xl' ? Math.max(i, Math.min(items.length, 60)) : i; }
    const shown = items.slice(0, Math.max(1, lim)), more = items.length - shown.length;
    const tile = (r, i) => { const src = imgOf(r), ar = arOf(r), ttl = String(r.prompt ?? r.title ?? r.caption ?? r.name ?? r.label ?? '').trim();
      const meta = [(r.width && r.height) ? r.width + '×' + r.height : '', r.source ?? r.kind ?? '', r.model ?? '', dayOf(r.created_at ?? r.created ?? r.when ?? '')].filter((x) => x && String(x).trim()).join(' · ');
      return '<figure class="vw-im' + (src ? '' : ' doc') + '"' + itemAttr(r, 'image') + ' data-tip="' + esc((ttl || 'image').slice(0, 160) + (meta ? '\n' + meta : '')) + '" style="flex:' + ar.toFixed(3) + ' 1 ' + Math.round(th * ar) + 'px;height:' + th + 'px">'
        + (src ? '<img src="' + esc(stAbs(src, o)) + '" alt="' + esc(ttl.slice(0, 80)) + '" loading="lazy" decoding="async">' : '<b>' + esc(String(r.kind ?? r.ext ?? r.type ?? 'file').replace(/_/g, ' ')) + '</b>')
        + (ttl ? '<figcaption>' + esc(ttl.slice(0, 70)) + '</figcaption>' : '') + (more > 0 && i === shown.length - 1 ? '<em>+ ' + more + '</em>' : '') + '</figure>'; };
    return wrap('images', '<div class="vw-ims" style="gap:' + gap + 'px">' + shown.map(tile).join('') + '<i class="vw-imfill"></i></div>', '', size === 'xl' ? 'overflow:auto' : '');
  };
  R.sprite = (d, H, o) => {
    if (typeof d !== 'string' && emptyEnv(d)) return noneRead(o);
    const r = typeof d === 'string' ? { sheet: d } : pickRec(d, o, (x) => spriteAnims(x).length > 0);
    const A = spriteAnims(r); if (!A.length) return EMPTY('a sprite needs a sheet (url · columns · count · fps) or its frames');
    const want = String(ui(o, 'anim', '') || (o && o.draw && o.draw.anim) || ''), a = A.find((x) => x.name === want) || A.find((x) => /^idle/i.test(x.name)) || A[0];
    const size = (o && o.size) || 'm', max = size === 'xl' ? 24 : 12, strip = A.length > 1 && !(o && o.draw && o.draw.strip === false), sh = strip ? (size === 'xl' ? 44 : 30) : 0;
    const W = (o && o.width) || ({ m: 300, l: 460, xl: 720 }[size] || 300), h = Math.max(24, Math.min(num(o && o.draw && o.draw.max) || 512, (H || HEIGHT[size] || 96) - sh - 24));
    const name = recName(r), play = (o && o.draw && o.draw.play) === 'hover' ? 'hover' : 'auto', meta = a.n + ' frames · ' + a.fps + ' fps' + (a.fw ? ' · ' + a.fw + '×' + a.fh : '');
    const stage = '<div class="vw-sprst vw-chk"' + itemAttr(spriteItem(r, a), 'sprite') + ' data-tip="' + esc((name ? name + ' · ' : '') + a.name + '\n' + meta) + '">' + spriteBox(a, h, o, play, '', W - 8) + '</div>';
    const head = '<div class="vw-sprcap"><b>' + esc(name || 'sprite') + '</b><span>' + esc(a.name) + '</span><small>' + esc(meta) + '</small></div>';
    const strp = strip ? '<div class="vw-sprstrip">' + A.slice(0, max).map((x) => '<button class="vw-sprt' + (x === a ? ' on' : '') + '"' + set('anim', x.name) + ' title="' + esc(x.name + ' · ' + x.n + ' frames · ' + x.fps + ' fps') + '">' + spriteBox(x, sh - 12, o, 'hover', '', 2 * (sh - 12)) + '<small>' + esc(x.name.slice(0, 16)) + '</small></button>').join('') + (A.length > max ? '<span class="vb-lbl">+ ' + (A.length - max) + '</span>' : '') + '</div>' : '';
    return wrap('sprite', stage + head + strp);
  };
  R.sprites = (d, H, o) => {
    const all = rows(d); if (!all.length) return EMPTY('a sprite library needs sprite records');
    const size = (o && o.size) || 'm', drawn = all.filter((r) => spriteAnims(r).length || imgOf(r)), rest = all.length - drawn.length; if (!drawn.length) return EMPTY(all.length + ' sprites · none has a sheet or frames yet');
    const ch = { m: 70, l: 84, xl: 104 }[size] || 70, cw = Math.round(ch * 0.95), gap = 6, W = (o && o.width) || ({ m: 300, l: 460, xl: 720 }[size] || 300);
    const cols = Math.max(1, Math.floor((W + gap) / (cw + gap))), fitR = Math.max(1, Math.floor(((H || HEIGHT[size] || 96) + gap - 14) / (ch + gap)));
    const lim = Math.round(num(o && o.draw && o.draw.limit)) || (size === 'xl' ? Math.min(drawn.length, 60) : cols * fitR);
    const card = (r) => { const A = spriteAnims(r), a = A.find((x) => /^idle/i.test(x.name)) || A[0], nm = recName(r) || 'unnamed', sub = [A.length ? A.length + (A.length === 1 ? ' anim' : ' anims') : 'still', r.sprite_size ? r.sprite_size + 'px' : ''].filter(Boolean).join(' · ');
      const img = a ? spriteBox(a, ch - 28, o, 'hover', '', cw - 8) : '<img class="vw-sprimg" src="' + esc(stAbs(imgOf(r), o)) + '" alt="" loading="lazy" decoding="async" style="height:' + (ch - 28) + 'px">';
      return '<div class="vw-sprc"' + itemAttr(a ? spriteItem(r, a) : charItem(r), 'sprite') + ' data-tip="' + esc(nm + '\n' + sub + (r.brief ? '\n' + String(r.brief).slice(0, 90) : '')) + '"><span class="vw-chk">' + img + '</span><b>' + esc(nm) + '</b><small>' + esc(sub) + '</small></div>'; };
    const left = drawn.length - Math.min(lim, drawn.length);
    return wrap('sprites', '<div class="vw-sprg" style="grid-template-columns:repeat(auto-fill,minmax(' + cw + 'px,1fr));gap:' + gap + 'px">' + drawn.slice(0, lim).map(card).join('') + '</div>'
      + ((left > 0 || rest > 0) ? cap([left > 0 ? '+ ' + left + ' more' : '', rest > 0 ? rest + ' with nothing drawn yet' : ''].filter(Boolean).join(' · ')) : ''), '', size === 'xl' ? 'overflow:auto' : '');
  };
  R.character = (d, H, o) => {
    const size = (o && o.size) || 'm', Hh = H || HEIGHT[size] || 96;
    if (emptyEnv(d)) return noneRead(o);
    if (o && o.draw && o.draw.roster) {
      const L = Array.isArray(d) ? d : ((d && typeof d === 'object') ? (Object.keys(d).map((k) => d[k]).find((v) => Array.isArray(v) && v.some((x) => x && typeof x === 'object' && !Array.isArray(x))) || [d]) : []);
      const all = rows(L); if (!all.length) return EMPTY('a roster needs character records');
      const ph = { m: 48, l: 64, xl: 84 }[size] || 48;
      return wrap('character', '<div class="vw-chrr" style="grid-template-columns:repeat(auto-fill,minmax(' + Math.round(ph * 2.6) + 'px,1fr))">' + all.slice(0, Math.round(num(o.draw.limit)) || 60).map((r) => { const u = imgOf(r), A = spriteAnims(r);
        const face = u ? '<img src="' + esc(stAbs(u, o)) + '" alt="" loading="lazy" decoding="async">' : (A.length ? spriteBox(A[0], ph - 4, o, 'hover', '', ph - 4) : '<b>' + esc(recName(r).slice(0, 1).toUpperCase() || '?') + '</b>');
        const chips = [r.style, r.render_mode, r.voice].filter(Boolean).slice(0, 3);
        return '<div class="vw-chrm"' + itemAttr(charItem(r), 'character') + ' data-tip="' + esc(recName(r) + (r.description || r.brief ? '\n' + String(r.description || r.brief).slice(0, 100) : '')) + '"><span class="vw-chrp vw-chk" style="width:' + ph + 'px;height:' + ph + 'px">' + face + '</span><span class="t"><b>' + esc(recName(r) || 'unnamed') + '</b><small>' + esc(String(r.description || r.brief || '').slice(0, 60)) + '</small><span class="vw-chips2">' + chips.map((c) => '<i>' + esc(String(c)) + '</i>').join('') + '</span></span></div>'; }).join('') + '</div>', '', 'overflow:auto');
    }
    const r = pickRec(d, o, (x) => !!(imgOf(x) || spriteAnims(x).length)); if (!r || typeof r !== 'object') return EMPTY('a character needs a record');
    const A = spriteAnims(r), u = imgOf(r), nm = recName(r) || 'character', about = String(r.description || r.brief || '').trim(), prompt = String(r.base_prompt || '').trim();
    const strips = Hh >= 190 ? 2 : (Hh >= 128 ? 1 : 0), sh = size === 'xl' ? 46 : 36, ph = Math.max(48, Math.min(200, Hh - strips * (sh + 6) - 4));
    const face = u ? '<img src="' + esc(stAbs(u, o)) + '" alt="' + esc(nm) + '" decoding="async">' : (A.length ? spriteBox(A.find((x) => /^idle/i.test(x.name)) || A[0], ph - 6, o, 'auto', '', ph - 6) : '<b>' + esc(nm.slice(0, 1).toUpperCase()) + '</b>');
    const traits = [['style', r.style], ['mode', r.render_mode], ['voice', r.voice], ['size', r.sprite_size ? r.sprite_size + 'px' : ''], ['colours', r.colors], ['palette', r.palette], ['tier', r.sd_tier_used],
      ['expressions', Array.isArray(r.states) && r.states.length ? r.states.length : ''], ['animations', A.length || '']].filter((x) => x[1] !== '' && x[1] != null && x[1] !== false);
    const extra = Array.isArray(r.traits) ? r.traits.map((x) => ['', x]) : (r.traits && typeof r.traits === 'object' ? Object.keys(r.traits).map((k) => [k, r.traits[k]]) : []);
    const chips = traits.concat(extra, (Array.isArray(r.tags) ? r.tags : []).map((x) => ['', x])).slice(0, size === 'xl' ? 14 : 8);
    const st = (r.stats && typeof r.stats === 'object') ? r.stats : ((r.attributes && typeof r.attributes === 'object') ? r.attributes : null), sk = st ? Object.keys(st).filter((k) => typeof st[k] === 'number') : [];
    const smax = num(o && o.draw && o.draw.max) || Math.max(10, ...sk.map((k) => st[k]));
    const bars = sk.length ? '<div class="vw-chrs">' + sk.slice(0, size === 'm' ? 3 : 6).map((k) => '<span' + itemAttr({ name: k, value: st[k], of: smax, character: nm }, 'stat') + '><em>' + esc(k) + '</em><i><u style="width:' + pct(st[k], smax).toFixed(1) + '%"></u></i><b>' + esc(fmt(st[k])) + '</b></span>').join('') + '</div>' : '';
    const fu = (r.frame_urls && typeof r.frame_urls === 'object') ? Object.keys(r.frame_urls).filter((k) => typeof r.frame_urls[k] === 'string' && r.frame_urls[k]) : [];
    const exprs = fu.length > 1 ? '<div class="vw-chrx">' + fu.slice(0, 12).map((k) => '<span class="vw-chk"' + itemAttr({ name: nm + ' · ' + k, state: k, character: nm, preview: r.frame_urls[k], prompt: (r.expression_prompts && r.expression_prompts[k]) || '' }, 'expression') + ' data-tip="' + esc(k) + '"><img src="' + esc(stAbs(r.frame_urls[k], o)) + '" alt="' + esc(k) + '" loading="lazy" decoding="async" style="height:' + (sh - 4) + 'px;width:' + (sh - 4) + 'px"></span>').join('') + '</div>' : '';
    const sprs = A.length ? '<div class="vw-chrx">' + A.slice(0, 12).map((a) => '<span class="vw-chk"' + itemAttr(spriteItem(r, a), 'sprite') + ' data-tip="' + esc(a.name + '\n' + a.n + ' frames · ' + a.fps + ' fps') + '">' + spriteBox(a, sh - 4, o, 'hover', '', 2 * sh) + '</span>').join('') + (A.length > 12 ? '<span class="vb-lbl">+ ' + (A.length - 12) + '</span>' : '') + '</div>' : '';
    const stripsH = [exprs, sprs].filter(Boolean).slice(0, strips).join('');
    return wrap('character', '<div class="vw-chr"><div class="vw-chrp vw-chk"' + itemAttr(charItem(r), 'character') + ' data-tip="' + esc(nm + (about ? '\n' + about.slice(0, 100) : '')) + '" style="width:' + ph + 'px;height:' + ph + 'px">' + face + '</div>'
      + '<div class="vw-chrt"><b>' + esc(nm) + '</b>' + (about || prompt ? '<p>' + esc((about || prompt).slice(0, 220)) + '</p>' : '')
      + (chips.length ? '<span class="vw-chips2">' + chips.map((c) => '<i' + (c[0] ? ' title="' + esc(c[0]) + '"' : '') + '>' + (c[0] ? '<small>' + esc(c[0]) + '</small> ' : '') + esc(String(c[1]).slice(0, 24)) + '</i>').join('') + '</span>' : '') + bars + '</div></div>' + stripsH);
  };
  /* the drawer's picture: an item that is an image shows it large; one that is a sprite (sheet · sheet_grid · frames ·
     fps, as spriteItem writes it) plays it */
  function drMedia(obj, host) {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return '';
    const o = { base: (host && host.base) || '' };
    if (typeof obj.sheet === 'string' && obj.sheet && /^\d+×\d+$/.test(String(obj.sheet_grid || ''))) { const g = String(obj.sheet_grid).split('×'), fr = String(obj.frame || '').split('×');
      const a = { name: String(obj.animation || ''), url: obj.sheet, frames: null, cols: +g[0] || 1, rows: +g[1] || 1, n: Math.max(1, num(obj.frames)), fps: Math.max(1, num(obj.fps) || 8), fw: +fr[0] || 0, fh: +fr[1] || 0, loop: true };
      return '<div class="vw-dr-media vw-chk">' + spriteBox(a, 180, o, 'auto', '', 300) + '</div>'; }
    const u = imgOf(obj); if (!u) return ''; const abs = stAbs(u, o);
    return '<div class="vw-dr-media vw-chk"><img src="' + esc(abs) + '" alt="" decoding="async"></div>' + (/^data:/.test(abs) ? '' : '<a class="vw-dr-open" href="' + esc(abs) + '" target="_blank" rel="noopener">open the full image ↗</a>');
  }
  const STUDIO_CSS = '.vb-images,.vb-sprite,.vb-sprites,.vb-character{align-self:stretch}'
    + '.vw-chk{background:repeating-conic-gradient(var(--b-s3,var(--bg3,#262b33)) 0 25%,var(--b-s2,var(--bg2,#1d2129)) 0 50%) 0 0/10px 10px}'
    + '.vw-spr{position:relative;display:inline-block;flex:none;vertical-align:middle}.vw-spr > i{position:absolute;inset:0;background-repeat:no-repeat;background-position:0 0;image-rendering:pixelated}'
    + '.vw-spr > img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;image-rendering:pixelated}.vw-spr > img.nx{opacity:0}'
    + '.vw-spr[data-play="hover"] > *{animation-play-state:paused!important}.vw-spr[data-play="hover"]:hover > *,.vw-sprc:hover .vw-spr > *,.vw-sprt:hover .vw-spr > *,.vw-chrx > span:hover .vw-spr > *{animation-play-state:running!important}.vw-spr[data-play="auto"]:hover > *{animation-play-state:paused!important}'
    + '@media (prefers-reduced-motion: reduce){.vw-spr > *{animation:none!important}}'
    + '.vw-ims{display:flex;flex-wrap:wrap;align-content:flex-start;width:100%}.vw-imfill{flex:1000 1 0;height:0}'
    + '.vw-im{margin:0;position:relative;overflow:hidden;border-radius:5px;background:var(--b-s2);cursor:pointer;min-width:0;box-shadow:inset 0 0 0 1px var(--b-bd)}.vw-im img{display:block;width:100%;height:100%;object-fit:cover;transition:transform .25s ease}'
    + '.vw-im:hover img{transform:scale(1.04)}.vw-im.hot{box-shadow:0 0 0 2px var(--b-ac)}.vw-im figcaption{position:absolute;left:0;right:0;bottom:0;padding:10px 6px 3px;font-size:10px;line-height:1.25;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.72));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:0;transition:opacity .15s}'
    + '.vw-im:hover figcaption{opacity:1}.vw-im.doc{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:6px;text-align:center}.vw-im.doc b{font-family:var(--b-mono);font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--b-ac)}.vw-im.doc figcaption{position:static;opacity:1;background:none;color:var(--b-t2);padding:0;white-space:normal;max-height:3.8em}'
    + '.vw-im em{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-style:normal;font-family:var(--b-mono);font-size:13px;color:#fff;background:rgba(0,0,0,.55)}'
    + '.vb-sprite{align-items:stretch;gap:5px}.vw-sprst{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;border-radius:6px;box-shadow:inset 0 0 0 1px var(--b-bd);cursor:pointer}'
    + '.vw-sprcap{display:flex;align-items:baseline;gap:6px;min-width:0;flex:none}.vw-sprcap b{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-sprcap span{font-size:10.5px;color:var(--b-ac)}.vw-sprcap small{margin-left:auto;font-family:var(--b-mono);font-size:10px;color:var(--b-t3);white-space:nowrap}'
    + '.vw-sprstrip{display:flex;gap:4px;overflow-x:auto;flex:none;scrollbar-width:thin}.vw-sprt{display:inline-flex!important;align-items:center;gap:4px;padding:2px 6px!important;border-radius:6px;background:var(--b-s2)!important;box-shadow:inset 0 0 0 1px var(--b-bd);flex:none}'
    + '.vw-sprt small{font-size:10px;color:var(--b-t2)}.vw-sprt.on{box-shadow:inset 0 0 0 1px var(--b-ac)}.vw-sprt.on small{color:var(--b-ac)}'
    + '.vw-sprg{display:grid;width:100%;align-content:start}.vw-sprc{display:flex;flex-direction:column;align-items:center;gap:1px;min-width:0;padding:4px;border-radius:7px;background:var(--b-s2);box-shadow:inset 0 0 0 1px var(--b-bd);cursor:pointer}'
    + '.vw-sprc > span{display:flex;align-items:center;justify-content:center;width:100%;flex:1;min-height:0;border-radius:5px;overflow:hidden}.vw-sprc b{font-size:11px;font-weight:600;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-sprc small{font-size:10px;color:var(--b-t3);white-space:nowrap}.vw-sprc.hot{box-shadow:inset 0 0 0 1px var(--b-ac)}'
    + '.vw-sprimg{max-width:100%;object-fit:contain;image-rendering:pixelated}'
    + '.vw-chr{display:flex;gap:10px;min-height:0;flex:1}.vw-chrp{flex:none;display:flex;align-items:center;justify-content:center;border-radius:8px;overflow:hidden;box-shadow:inset 0 0 0 1px var(--b-bd);cursor:pointer}.vw-chrp img{width:100%;height:100%;object-fit:contain}.vw-chrp > b{font-size:22px;color:var(--b-t2)}'
    + '.vw-chrt{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px;overflow:hidden}.vw-chrt > b{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-chrt p{margin:0;font-size:11px;line-height:1.35;color:var(--b-t2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}'
    + '.vw-chips2{display:flex;flex-wrap:wrap;gap:3px}.vw-chips2 i{font-style:normal;font-size:10px;padding:1px 7px;border-radius:99px;background:var(--b-s3);color:var(--b-t1);white-space:nowrap}.vw-chips2 i small{color:var(--b-t3);font-size:10px}'
    + '.vw-chrs{display:flex;flex-direction:column;gap:2px}.vw-chrs > span{display:grid;grid-template-columns:minmax(0,70px) 1fr auto;gap:6px;align-items:center;font-size:10px}.vw-chrs em{font-style:normal;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-chrs i{height:6px;border-radius:3px;background:var(--b-s3);overflow:hidden}.vw-chrs u{display:block;height:100%;background:var(--b-ac)}.vw-chrs b{font-family:var(--b-mono);font-weight:400}'
    + '.vw-chrx{display:flex;gap:4px;overflow-x:auto;flex:none;scrollbar-width:thin}.vw-chrx > span{flex:none;display:flex;align-items:center;justify-content:center;padding:2px;border-radius:5px;box-shadow:inset 0 0 0 1px var(--b-bd);cursor:pointer}.vw-chrx img{object-fit:contain;display:block}.vw-chrx > span.hot{box-shadow:inset 0 0 0 1px var(--b-ac)}'
    + '.vw-chrr{display:grid;gap:6px;width:100%;align-content:start}.vw-chrm{display:flex;gap:7px;align-items:center;min-width:0;padding:4px;border-radius:8px;background:var(--b-s2);box-shadow:inset 0 0 0 1px var(--b-bd);cursor:pointer}.vw-chrm.hot{box-shadow:inset 0 0 0 1px var(--b-ac)}'
    + '.vw-chrm .t{display:flex;flex-direction:column;gap:2px;min-width:0}.vw-chrm .t b{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-chrm .t small{font-size:10px;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.vw-dr-media{display:flex;align-items:center;justify-content:center;min-height:120px;max-height:360px;border-radius:8px;overflow:hidden;margin:4px 0 6px;box-shadow:inset 0 0 0 1px var(--border,rgba(255,255,255,.1))}.vw-dr-media img{display:block;max-width:100%;max-height:360px;object-fit:contain}'
    + '.vw-dr-open{display:inline-block;margin:0 0 8px;font-size:11px;color:var(--acc,#6ea8d8);text-decoration:none}'
    + '.vw-im.hot,.vw-sprst.hot,.vw-chrp.hot,.vw-chrx > span.hot{filter:none}'
    /* blocks off: the cards lose their ground - on the page, and in the element's shadow on a rule of its own (a selector
       list holding :host-context is dropped whole where :host-context means nothing, on the page) */
    + 'html[data-blocks="off"] .vw-sprc,html[data-blocks="off"] .vw-chrm,html[data-blocks="off"] .vw-sprt{background:transparent!important}'
    + ':host-context(html[data-blocks="off"]) .vw-sprc,:host-context(html[data-blocks="off"]) .vw-chrm,:host-context(html[data-blocks="off"]) .vw-sprt{background:transparent!important}';
  // the registrations: the forms drawn, their shapes, their sample faces, their glyphs; each takes its body whole at L and XL
  Object.assign(DRAWN, { images: 'items', sprites: 'items', sprite: 'string', character: 'string' });
  ['images', 'sprites', 'sprite', 'character'].forEach((f) => TABLE_FORMS.add(f));
  Object.assign(GLYPH, { images: '▣', sprites: '◳', sprite: '◳', character: '☺' });
  /* the sample faces: drawn here, so the gallery and the pickers show the forms with nothing read - a pixel figure's
     sheet (four frames of idle, six of walk) and a few pictures of different shapes */
  const SPR_SAMPLE = (() => { const fig = (k, n, walk) => { const bob = walk ? 0 : (k % 2), sw = walk ? Math.round(Math.sin(k / n * Math.PI * 2) * 2) : 0, x = k * 16;
      const px = (c, xx, yy, w, h) => "<rect x='" + (x + xx) + "' y='" + (yy + bob) + "' width='" + w + "' height='" + h + "' fill='" + c + "'/>";
      return px('#f2c89b', 5, 1, 6, 5) + px('#2b2f3a', 6, 3, 1, 1) + px('#2b2f3a', 9, 3, 1, 1) + px('#5aa0e8', 4, 6, 8, 5) + px('#f2c89b', 3 - Math.max(0, sw), 7, 1, 3) + px('#f2c89b', 12 + Math.min(0, sw), 7, 1, 3) + px('#3a4b6b', 5 + sw, 11, 2, 4) + px('#3a4b6b', 9 - sw, 11, 2, 4); };
    const sheet = (n, walk) => 'data:image/svg+xml,' + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' width='" + (16 * n) + "' height='16' shape-rendering='crispEdges'>" + Array.from({ length: n }, (_, k) => fig(k, n, walk)).join('') + '</svg>');
    return { idle: { url: sheet(4, false), columns: 4, rows: 1, count: 4, fps: 4, frame_width: 16, frame_height: 16, loop: true }, walk: { url: sheet(6, true), columns: 6, rows: 1, count: 6, fps: 8, frame_width: 16, frame_height: 16, loop: true } }; })();
  const PIC = (w, h, a, b, t) => 'data:image/svg+xml,' + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' width='" + w + "' height='" + h + "'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='" + a + "'/><stop offset='1' stop-color='" + b + "'/></linearGradient></defs><rect width='100%' height='100%' fill='url(#g)'/>" + (t === 'sun' ? "<circle cx='" + w * .7 + "' cy='" + h * .35 + "' r='" + Math.min(w, h) * .14 + "' fill='#ffe3a3'/><path d='M0 " + h + " L" + w * .35 + ' ' + h * .55 + ' L' + w * .6 + ' ' + h * .8 + ' L' + w * .8 + ' ' + h * .6 + ' L' + w + ' ' + h + " Z' fill='#1d2433' opacity='.8'/>" : "<circle cx='" + w / 2 + "' cy='" + h / 2 + "' r='" + Math.min(w, h) * .28 + "' fill='#ffffff' opacity='.25'/>") + '</svg>');
  Object.assign(FORM_SAMPLE, {
    images: () => [[512, 512, '#6ea8d8', '#a78bfa', 'sun', 'a lighthouse at dusk, cinematic'], [768, 512, '#e09a55', '#5a2b6b', 'sun', 'desert canyon, golden hour'], [512, 768, '#5ec9a0', '#1f3b4d', '', 'portrait of a fox in a scarf'], [512, 512, '#e07a9a', '#2b2f3a', '', 'cyber sloth, anime'], [640, 480, '#3585c9', '#0b1119', 'sun', 'harbour at night'], [512, 512, '#bb881a', '#54a863', '', 'a duck, watercolour'], [768, 432, '#866ec5', '#00aba4', 'sun', 'aurora over a lake']].map((r, i) => ({ url: PIC(r[0] / 8, r[1] / 8, r[2], r[3], r[4]), prompt: r[5], width: r[0], height: r[1], source: i === 3 ? 'img2img' : 'txt2img', created_at: '2026-09-2' + (7 - (i % 5)) + 'T1' + i + ':00:00Z' })),
    sprite: () => ({ name: 'Pip', char_id: 'sample', style: 'pixel', sprite_size: 16, sheet: { animations: SPR_SAMPLE } }),
    sprites: () => ['Pip', 'Moss', 'Rook', 'Wren', 'Ash', 'Bolt'].map((n, i) => ({ name: n, char_id: 's' + i, style: 'pixel', sprite_size: 16, sheet: { animations: i % 2 ? { walk: SPR_SAMPLE.walk, idle: SPR_SAMPLE.idle } : { idle: SPR_SAMPLE.idle, walk: SPR_SAMPLE.walk } } })),
    character: () => ({ display_name: 'Vera', agent_id: 'sample', description: 'the house assistant: calm, exact, a little dry', style: 'pixel', render_mode: 'spritesheet', voice: 'af_heart', states: ['neutral', 'talking', 'thinking', 'happy'],
      frame_urls: { neutral: PIC(64, 64, '#6ea8d8', '#2b2f3a', ''), talking: PIC(64, 64, '#5ec9a0', '#2b2f3a', ''), thinking: PIC(64, 64, '#a78bfa', '#2b2f3a', ''), happy: PIC(64, 64, '#e09a55', '#2b2f3a', '') },
      stats: { warmth: 7, focus: 9, humour: 5 }, sheet: { animations: SPR_SAMPLE } }),
  });

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
      skin: String(o.skin || 'inherit').toLowerCase(), subject: String(o.subject || ''), projection: String(o.projection || drawIn.proj || drawIn.projection || '').toLowerCase(), actions: Array.isArray(o.actions) ? o.actions : ['dive', 'pin', 'ask'],
      children: Array.isArray(o.children) ? o.children : undefined, layout: o.layout, data: o.data };
  }
  const readable = (cap) => /(\.(get|list|status|load|history|read|stats|metrics|recent|tail|search|find|show|info|summary|query|health|state|series|events|nodes|jobs|runs|snapshot|top|instances|sources|request_log|keys|results|installed|config|models|list_models|route_stats|embed_config)|_stats$|^obs\.|^sysmon\.|^perf\.|^nodes\.|^docker\.(ps|stats)|^git\.log|^markets\.|^redis\.|^proxmox\.|^mesh\.|^estate\.|^backup\.|^bench\.|^catalog\.|^background\.|^topology\.|^jobs\.|^memory\.stats|^ollama\.(gate\.status|instances|list_models|route_stats|request_log|routing\.get|embed_config|model_tags\.get)|^evolve\.(sandbox\.list|pipeline\.list|activity|tasks\.overview|unittest\.history|tests\.matrix|mission\.events|agents\.rows|authors|ship\.branches|git\.graph)$|^ci\.(matrix|race|tests|pulse|fleet|board|census|compare|track)$|^loop\.ci\.(matrix|race|board|perf)$|^census\.(runs|landed|live|board)$|^activity\.(sessions|pipelines)$|^syslog\.errors$|^dream\.(sensor\.cap_calls|last|hitl\.pending)$|^memory\.graph_full$|^cal\.(events|todos|notes)\.list$)/.test(cap) && !/(write|delete|remove|create|run|exec|kill|restart|stop|start|set|save|send|post|push|upsert|pull|install|activate|acquire|release|enqueue|cancel|spawn|prune|reap)\b/.test(cap);   // the dashboard's own readings read on their own (route_stats / results / config tails were left waiting for a click)
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
    /* a structured graph (the Explode contract): the estate's own embed, handed the contract the widget already
       holds — setDoc, never a re-fetch, because the widget's source has done the reading (§3.6a) */
    const sgs = R0.querySelectorAll ? R0.querySelectorAll('.vw-sg[data-sg]:not([data-live])') : [];
    if (sgs.length && window.customElements && customElements.get('vera-graph-embed')) sgs.forEach((slot) => { slot.dataset.live = '1';
      let doc = null; try { doc = JSON.parse(slot.dataset.sg || 'null'); } catch (_) {}
      const el = document.createElement('vera-graph-embed');
      String(slot.dataset.sgAttrs || '').split(' ').filter(Boolean).forEach((a) => { const i = a.indexOf('='); if (i < 0) return;
        try { el.setAttribute(a.slice(0, i), a.slice(i + 1).replace(/^"|"$/g, '')); } catch (_) {} });
      el.setAttribute('bare', ''); el.style.cssText = 'position:absolute;inset:0';
      slot.innerHTML = ''; slot.appendChild(el);
      try { if (doc && typeof el.setDoc === 'function') el.setDoc(doc); } catch (_) {} n++; });
    const slots = R0.querySelectorAll ? R0.querySelectorAll('.vw-mm[data-mm-code]:not([data-live])') : [];
    if (!slots.length || !(window.customElements && customElements.get('vera-mermaid'))) return n;
    slots.forEach((slot) => { slot.dataset.live = '1'; const el = document.createElement('vera-mermaid'); el.setAttribute('bare', ''); el.setAttribute('fill', ''); slot.innerHTML = ''; slot.appendChild(el); try { el.render(slot.dataset.mmCode); } catch (_) {} n++; });
    return n;
  }

  /* ── the styles (host-injected once; the element carries them in its shadow) ── */
  const CSS = fontScale(STUDIO_CSS + `
.wempty{color:var(--dim2,#8a92a0);font-size:9.5px;font-family:var(--mono,ui-monospace,monospace)}
.vw-nodata{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;width:100%;height:100%;min-height:34px;text-align:center}
.vw-nodata > b{font-family:var(--mono,ui-monospace,monospace);font-size:10.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--dim2,#8a92a0)}
.vw-nodata > i{font-style:normal;font-family:var(--mono,ui-monospace,monospace);font-size:8.5px;color:var(--dim,#6b7280);opacity:.9;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
span.vw-nodata{color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);font-size:10px}
.vw-sampled{position:relative;width:100%;min-width:0;display:flex;flex-direction:column}.vw-sampled > .vw-sampletag{display:block;align-self:flex-end;flex:none;height:10px;line-height:10px;margin-bottom:1px;font-style:normal;font-family:var(--mono,ui-monospace,monospace);font-size:7.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--acc3,#d4a96a);opacity:.85;pointer-events:none}
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
.vw-hero{display:flex;align-items:baseline;gap:4px;max-width:100%;min-width:0}.vw-hero b{font-size:clamp(16px, 62cqh, 26px);font-weight:600;letter-spacing:-.02em;line-height:1;color:var(--text,#d8dce4);font-family:var(--mono,ui-monospace,monospace);white-space:nowrap}.vw-hero .vw-unit{color:var(--dim2,#8a92a0);font-size:clamp(9px, 26cqh, 12px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vw-delta{font-size:9.5px;font-family:var(--mono,ui-monospace,monospace);margin-left:6px}.vw-delta.up{color:var(--acc2,#8fb87a)}.vw-delta.down{color:var(--err,#c96b6b)}
.vw-meter{display:flex;align-items:center;gap:8px;width:100%}.vw-track{flex:1;height:8px;border-radius:4px;background:var(--bg2,#1a1c20);overflow:hidden;display:block}.vw-track i{display:block;height:100%;background:var(--acc,#5a9e8f);border-radius:4px}.vw-meter b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--text,#d8dce4)}
.vw-therm{display:flex;flex-direction:column;gap:4px;width:100%;font-size:9.5px}.vw-therm div{display:grid;grid-template-columns:90px 1fr 48px;gap:6px;align-items:center}.vw-therm label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text,#d8dce4)}.vw-therm span{display:block;height:8px;border-radius:4px;background:var(--bg2,#1a1c20);overflow:hidden}.vw-therm span i{display:block;height:100%;background:var(--acc,#5a9e8f);border-radius:4px}.vw-therm b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;text-align:right;color:var(--text,#d8dce4)}
.vw-heat{display:grid;gap:2px;width:100%}.vw-heat div{aspect-ratio:1.6;border-radius:3px;background:var(--acc,#5a9e8f);display:flex;align-items:flex-end;padding:2px 4px;font-family:var(--mono,ui-monospace,monospace);font-size:8px;color:var(--text,#d8dce4);overflow:hidden}
.vw-matrix{display:grid;gap:2px;width:100%;font-size:8.5px;font-family:var(--mono,ui-monospace,monospace);align-items:center}.vw-matrix i{color:var(--dim2,#8a92a0);text-align:center;font-style:normal;overflow:hidden;white-space:nowrap}.vw-matrix b{color:var(--text,#d8dce4);font-weight:400;padding-right:6px;white-space:nowrap}.vw-matrix span{display:block;height:14px;border-radius:2px}
.vw-donut{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:6px 12px;width:100%;height:100%}
.vw-donut .vw-dtot{font-family:var(--mono,ui-monospace,monospace);font-size:13px;font-weight:600;fill:var(--text,#d8dce4)}
.vw-legend.vw-legend-col{flex-direction:column;flex-wrap:nowrap;gap:3px;flex:0 1 auto;max-width:100%}
.vw-legend-col span{display:grid;grid-template-columns:8px minmax(0,auto) auto auto;gap:6px;align-items:center}
.vw-legend-col em{font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vw-legend-col b{margin-left:0}
.vw-legend-col small{font-family:var(--mono,ui-monospace,monospace);font-size:9px;color:var(--dim,#6b7280)}.vw-legend{display:flex;flex-direction:column;gap:2px;font-size:9.5px;min-width:0}
.vw-svgt{font-size:9px;font-family:var(--mono,monospace)}.vw-legend span{display:flex;align-items:center;gap:5px;white-space:nowrap;overflow:hidden;color:var(--text,#d8dce4)}.vw-legend i{width:8px;height:8px;border-radius:50%;flex-shrink:0}.vw-legend b{margin-left:auto;font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--dim2,#8a92a0)}
.vw-stack{display:flex;flex-direction:column;gap:6px;width:100%}.vw-stackbar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--bg2,#1a1c20)}.vw-stackbar i{display:block;height:100%}.vw-stack .vw-legend{flex-direction:row;flex-wrap:wrap;gap:4px 10px}
.vw-pills{display:flex;flex-wrap:wrap;gap:5px;width:100%;height:100%;align-content:space-evenly}.vw-pill{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:12px;border:1px solid var(--border,rgba(255,255,255,.09));background:var(--bg2,#1a1c20);font-size:9.5px}.vw-pill small{color:var(--dim2,#8a92a0)}.vw-pill b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--text,#d8dce4)}.vw-pill.ok b{color:var(--acc2,#8fb87a)}.vw-pill.bad b{color:var(--err,#c96b6b)}
.vw-log{display:flex;flex-direction:column;gap:2px;width:100%;font-family:var(--mono,ui-monospace,monospace);font-size:9px}.vw-log div{display:flex;gap:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text,#d8dce4)}.vw-log .t{color:var(--dim,#6b7280);flex-shrink:0}.vw-log .k{color:var(--acc,#5a9e8f);flex-shrink:0;min-width:48px}.vw-log .lane{color:var(--dim2,#8a92a0);text-transform:uppercase;letter-spacing:.08em;font-size:8px;margin-top:3px}
.vw-tablewrap{width:100%;overflow:auto}.vw-tablewrap table{border-collapse:collapse;font-size:9.5px;width:100%}.vw-tablewrap th{text-align:left;font-family:var(--mono,ui-monospace,monospace);font-size:8px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#6b7280);padding:2px 6px;border-bottom:1px solid var(--border2,rgba(255,255,255,.18))}.vw-tablewrap td{padding:2px 6px;border-bottom:1px solid var(--border,rgba(255,255,255,.09));white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;color:var(--text,#d8dce4)}
.vw-list{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-list div{display:flex;justify-content:space-between;gap:8px;overflow:hidden;white-space:nowrap;color:var(--text,#d8dce4)}.vw-list b{font-weight:500;overflow:hidden;text-overflow:ellipsis}.vw-list small{color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);flex-shrink:0}
.vw-check{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-check div{display:flex;gap:6px;align-items:baseline;color:var(--text,#d8dce4)}.vw-check i{font-style:normal;color:var(--dim2,#8a92a0)}.vw-check .on i{color:var(--acc2,#8fb87a)}.vw-check .on span{color:var(--dim2,#8a92a0);text-decoration:line-through}.vw-check small{margin-left:auto;color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-steps{display:flex;flex-direction:column;gap:3px;width:100%;font-size:10px}.vw-steps div{display:flex;gap:7px;align-items:center;color:var(--text,#d8dce4)}.vw-steps i{font-style:normal;width:16px;height:16px;border-radius:50%;border:1px solid var(--border2,rgba(255,255,255,.18));display:inline-flex;align-items:center;justify-content:center;font-size:8px;font-family:var(--mono,ui-monospace,monospace);color:var(--dim2,#8a92a0);flex-shrink:0}.vw-steps .done i{background:var(--acc2,#8fb87a);border-color:var(--acc2,#8fb87a);color:#0e0f12}.vw-steps .now i{border-color:var(--acc,#5a9e8f);color:var(--acc,#5a9e8f);box-shadow:0 0 0 2px color-mix(in srgb,var(--acc,#5a9e8f) 25%,transparent)}.vw-steps .bad i{border-color:var(--err,#c96b6b);color:var(--err,#c96b6b)}.vw-steps small{margin-left:auto;color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-cal{display:flex;gap:8px;width:100%;overflow:auto;font-size:9.5px}.vw-cal .day{min-width:110px;display:flex;flex-direction:column;gap:2px}.vw-cal b{font-family:var(--mono,ui-monospace,monospace);font-weight:400;color:var(--dim2,#8a92a0);font-size:8.5px;text-transform:uppercase;letter-spacing:.06em}.vw-cal span{display:flex;gap:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text,#d8dce4)}.vw-cal small{color:var(--dim,#6b7280);font-family:var(--mono,ui-monospace,monospace)}
.vw-str{white-space:pre-wrap;font-size:10.5px;line-height:1.5;color:var(--text,#d8dce4);width:100%}
.vw-mm{width:100%;min-height:70px;height:100%;display:flex;align-items:center;justify-content:center}.vw-mm small{color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);font-size:9px}.vw-mm vera-mermaid{display:block;width:100%;height:100%}
.vw-panel{width:100%;border:none;background:transparent;display:block}
/* a structured graph: the slot the estate's embed is mounted into, and the counts it stands in for until then */
.vw-sg{width:100%;display:block;overflow:hidden}
.vw-sg vera-graph-embed{display:block;width:100%;height:100%}
.vw-sg>.vw-sg-w{position:absolute;left:7px;top:6px;color:var(--dim2,#8a92a0);font-size:9.5px;font-family:var(--mono,ui-monospace,monospace)}
.vw-l{display:grid;grid-template-columns:1fr 160px;gap:10px;width:100%;align-items:start}.vw-xl{display:grid;grid-template-columns:1fr 180px;gap:10px;width:100%;align-items:start}.vw-xl .vw-xltable{grid-column:1/-1}
.vw-main{min-width:0}.vw-detail{display:flex;flex-direction:column;gap:3px;font-size:9.5px;border-left:1px solid var(--border,rgba(255,255,255,.09));padding-left:10px}.vw-detail div{display:flex;justify-content:space-between;gap:8px;color:var(--text,#d8dce4)}.vw-detail span{color:var(--dim2,#8a92a0);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vw-detail b{font-family:var(--mono,ui-monospace,monospace);font-weight:400}
.vw-comp{display:grid;gap:8px;width:100%;grid-template-columns:1fr 1fr}.vw-comp-rows,.vw-comp-report{grid-template-columns:1fr}.vw-comp-report{overflow-y:auto;align-content:start}.vw-comp-report .vw-slot-block{flex-shrink:0}.vw-comp-report .vw-slot-block .vw-slot-b{overflow:visible}.vw-comp-rail{grid-template-columns:1fr}.vw-comp-2x2{grid-template-columns:1fr 1fr}
.vw-slot{min-width:0;min-height:0;box-sizing:border-box;overflow:hidden;background:var(--surf2,var(--bg2,#1a1c20));border-radius:var(--r-sm,6px);padding:7px 9px 8px;display:flex;flex-direction:column;gap:4px;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}
.vw-slot-h{display:flex;align-items:baseline;gap:6px;font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:var(--t3,var(--dim,#6b7280));flex-shrink:0;white-space:nowrap;overflow:hidden}.vw-slot-h b{margin-left:auto;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:11px;color:var(--t1,var(--text,#d8dce4));font-weight:400;text-transform:none;letter-spacing:0}
.vw-slot-b{flex:1;min-height:0;display:flex;align-items:safe center;overflow:auto}.vw-slot-b > *{width:100%}
.vw-slot-row{flex-direction:row;align-items:center;gap:8px;background:transparent;box-shadow:none;border-radius:0;padding:3px 0;border-bottom:1px solid var(--bd,var(--border,rgba(255,255,255,.09)))}
.vw-slot.vw-stale .vw-slot-b,.vw-slot-row.vw-stale .vw-chip{opacity:.55}.vw-kerr,.vw-kempty{font-style:normal;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:7.5px;letter-spacing:.06em;text-transform:uppercase;margin-left:6px;opacity:.85}.vw-kerr{color:var(--err,#c96b6b)}.vw-kempty{color:var(--t3,var(--dim,#6b7280))}
.vw-kread{font-style:normal;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:8px;letter-spacing:.04em;color:var(--t3,var(--dim,#6b7280));opacity:.8;margin-left:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:vw-kread 1.6s ease-in-out infinite}@keyframes vw-kread{50%{opacity:.35}}
.vw-root[data-stale="1"] .vw-body{opacity:.55}.vw-slot-row .k{width:84px;flex-shrink:0;font-size:var(--label-size,9px);text-transform:var(--label-case,uppercase);letter-spacing:var(--label-track,.06em);font-weight:var(--label-weight,600);color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vw-slot-row .vw-slot-c{flex:1;min-width:0;display:flex;align-items:center;overflow:hidden}.vw-slot-row .vw-slot-c > *{max-width:100%}
.vw-comp-rows,.vw-comp-report,.vw-comp-rail{display:flex;flex-direction:column;gap:0;align-content:start;height:100%;justify-content:space-evenly}.vw-slot-row{min-height:22px;padding:1px 0;flex:0 0 auto}.vw-slot-row:last-child{border-bottom:none}
.vw-chips{display:inline-flex;flex-wrap:nowrap;gap:4px;min-width:0;max-width:100%;overflow:hidden}.vw-chips .vw-chip{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}.vw-chips .vw-chip small{overflow:hidden;text-overflow:ellipsis}.vw-chip .st{width:6px;height:6px;border-radius:50%;flex-shrink:0;display:inline-block}.vw-chip.more{color:var(--dim2,#8a92a0)}
.vw-kv .more{color:var(--dim2,#8a92a0);font-size:8.5px}
.vb-small-multiples{display:flex;flex-direction:column;height:100%;justify-content:space-evenly}
@media (max-width:520px){.vw-l,.vw-xl{grid-template-columns:1fr}.vw-detail{border-left:none;padding-left:0}}
/* -- the style pack (data-style on the page root, seen through :host-context): labels take its case, tracking and weight;
   pixel's figures its display face, its bars striped and its lines hard; newspaper rules under a slot's head; terminal is
   outlines and hatching, not floods; every pack's radius comes through --r-sm -- */
.vw-slot-h,.vw-hd,.vw-log .lane,.vw-tablewrap th{text-transform:var(--label-case,uppercase);letter-spacing:var(--label-track,.08em);font-weight:var(--label-weight,600);font-family:var(--f-ui,var(--sans,system-ui,sans-serif))}
:host-context([data-style="pixel"]) .vw-slot{box-shadow:0 0 0 2px var(--bd2,rgba(255,255,255,.14))}
:host-context([data-style="pixel"]) .vw-hero b,:host-context([data-style="pixel"]) .vb-hero b,:host-context([data-style="pixel"]) .vb-bigs b{font-family:var(--f-disp,var(--f-mono,ui-monospace,monospace));letter-spacing:0;font-size:clamp(11px, min(42cqh, 14cqi), 20px)}
:host-context([data-style="pixel"]) .vw-slot-h b{font-family:var(--f-mono,ui-monospace,monospace);font-size:12px}
:host-context([data-style="pixel"]) .vw-track,:host-context([data-style="pixel"]) .vw-track i,:host-context([data-style="pixel"]) .vw-therm span,:host-context([data-style="pixel"]) .vw-therm span i,:host-context([data-style="pixel"]) .vb-rw .tr,:host-context([data-style="pixel"]) .vb-rw .tr i,:host-context([data-style="pixel"]) .vw-stackbar,:host-context([data-style="pixel"]) .vw-stackbar i,:host-context([data-style="pixel"]) .vw-pill,:host-context([data-style="pixel"]) .vw-legend i,:host-context([data-style="pixel"]) .vb-batt .cells i,:host-context([data-style="pixel"]) .vw-heat div{border-radius:0}
:host-context([data-style="pixel"]) .vw-track i,:host-context([data-style="pixel"]) .vw-therm span i,:host-context([data-style="pixel"]) .vb-rw .tr i,:host-context([data-style="pixel"]) .vw-stackbar i,:host-context([data-style="pixel"]) .vb-batt .cells i.on{background-image:repeating-linear-gradient(90deg,rgba(0,0,0,.3) 0 1px,transparent 1px 4px)}
:host-context([data-style="pixel"]) polyline,:host-context([data-style="pixel"]) polygon{stroke-width:3;stroke-linejoin:miter;stroke-linecap:butt;shape-rendering:crispEdges}
:host-context([data-style="pixel"]) circle,:host-context([data-style="pixel"]) rect{shape-rendering:crispEdges}
:host-context([data-style="newspaper"]) .vw-slot{background:transparent;box-shadow:0 0 0 1px var(--bd,rgba(255,255,255,.09))}
:host-context([data-style="newspaper"]) .vw-slot-h{border-bottom:1px solid var(--t3,#6b7280);padding-bottom:3px}
:host-context([data-style="terminal"]) .vw-slot{background:transparent;box-shadow:0 0 0 1px var(--bd,rgba(255,255,255,.09))}
:host-context([data-style="terminal"]) .vw-track i,:host-context([data-style="terminal"]) .vw-therm span i,:host-context([data-style="terminal"]) .vb-rw .tr i,:host-context([data-style="terminal"]) .vw-stackbar i{background-image:repeating-linear-gradient(135deg,rgba(0,0,0,.38) 0 2px,transparent 2px 5px)}
:host-context([data-style="terminal"]) .vw-pill{background:transparent;box-shadow:inset 0 0 0 1px var(--bd2,rgba(255,255,255,.14))}
/* ── the boards' forms (vb-): the token bridge, then the Widgets board's CSS under its own prefix ── */

/* ── the iso and motion forms (the WidgetsMotion and WidgetsIso boards): every face a positioned div with a clip-path ── */
.vb-isow{position:relative;overflow:hidden;flex:none;max-width:100%;margin:0 auto}
.vb-isow i.f{position:absolute;display:block}.vb-isow i.f.t{box-shadow:inset 0 0 0 1px rgba(255,255,255,.10)}
.vb-isow .isoe{position:absolute;height:2px;transform-origin:0 50%;border-radius:1px;opacity:.9}
.vb-isow .isol{position:absolute;font-family:var(--b-mono);font-size:10px;color:var(--b-t2);white-space:nowrap;transform:translate(-50%,-100%);text-shadow:0 1px 3px var(--bg,var(--bg0,#0e0f12)),0 0 6px var(--bg,var(--bg0,#0e0f12));letter-spacing:-.01em;pointer-events:none}
.vb-isow .isol.sm{font-size:8.5px}.vb-isow .isol.hd{color:var(--b-t1);font-weight:600;font-family:var(--b-ui);font-size:10.5px}.vb-isow .isol.dn{transform:translate(-50%,0)}.vb-isow .isol.big{font-family:var(--b-mono);font-size:26px;font-weight:700;color:var(--b-t1);letter-spacing:-.03em}
.vb-isow .isoe.needle{height:3px;background:var(--b-t1)!important;box-shadow:0 0 8px 1px color-mix(in srgb,var(--b-ac3) 70%,transparent);z-index:3}
.vb-isowait{align-items:center;justify-content:center;min-height:60px}
/* the effects a box carries: hot · lamp · vu · mv · shine · wave · bub · flowe */
@keyframes vb-hot{0%,100%{filter:brightness(1)}50%{filter:brightness(1.7)}}.vb-isow i.f.hot{animation:vb-hot 1.3s ease-in-out infinite;box-shadow:0 0 12px 2px color-mix(in srgb,var(--b-ac4) 55%,transparent)}
@keyframes vb-blink{0%,49%{opacity:1}50%,100%{opacity:.12}}.vb-isow i.f.lamp{animation:vb-blink 1s steps(1) infinite;box-shadow:0 0 8px 1px color-mix(in srgb,var(--b-ac4) 70%,transparent)}
@keyframes vb-vu{0%,100%{opacity:1}50%{opacity:.15}}.vb-isow i.f.vu{animation:vb-vu .9s steps(1) infinite;animation-delay:calc(var(--n,0) * -.13s)}
@keyframes vb-conv{0%{transform:translate(0,0);opacity:0}6%{opacity:1}94%{opacity:1}100%{transform:translate(var(--cx),var(--cy));opacity:0}}.vb-isow i.f.mv{animation:vb-conv 6s linear infinite;animation-delay:calc(var(--n,0) * -2s)}
@keyframes vb-shine{0%,78%,100%{filter:brightness(1)}88%{filter:brightness(1.6)}}.vb-isow i.f.shine{animation:vb-shine 3.2s ease-in-out infinite;animation-delay:calc(var(--n,0) * .35s)}
@keyframes vb-sweep{to{transform:rotate(360deg)}}
.vb-isow i.f.wave.t{background-image:repeating-linear-gradient(90deg,color-mix(in srgb,#fff 22%,transparent) 0 7px,transparent 7px 17px)!important;animation:vb-wave 2.6s linear infinite}@keyframes vb-wave{to{background-position:34px 0}}
.vb-isow i.f.bub{animation:vb-bub 3.4s ease-in infinite;animation-delay:calc(var(--n,0) * -.7s);opacity:.75}@keyframes vb-bub{0%{transform:translateY(0);opacity:0}12%{opacity:.8}100%{transform:translateY(-58px);opacity:0}}
.vb-isow .isoe.flowe{height:5px;border-radius:3px;background-image:repeating-linear-gradient(90deg,color-mix(in srgb,#fff 55%,transparent) 0 6px,transparent 6px 15px)!important;animation:vb-flowe 1.1s linear infinite}@keyframes vb-flowe{to{background-position:21px 0}}
/* the radar: a disc on the floor, rotated by the azimuth, squashed by the tilt; a ping per event */
.vb-isow .radar{position:absolute;transform-origin:50% 50%}.vb-isow .radar .disc{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--b-ac2) 16%,transparent),transparent 72%);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--b-ac2) 40%,transparent)}
.vb-isow .radar .ring2{position:absolute;inset:26%;border-radius:50%;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--b-ac2) 30%,transparent)}.vb-isow .radar .beam{position:absolute;inset:0;border-radius:50%;animation:vb-sweep 3.2s linear infinite;background:conic-gradient(from 0deg,transparent 0deg,color-mix(in srgb,var(--b-ac2) 60%,transparent) 42deg,transparent 43deg)}
.vb-isow .ping{position:absolute;width:6px;height:6px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 6px 1px currentColor;animation:vb-blink 1.6s ease-in-out infinite;animation-delay:calc(var(--n,0) * .2s)}
/* the iso frame: real markup on the plane */
.vb-isow .xif{position:absolute;transform-origin:0 0;z-index:9;background:var(--b-s2);box-shadow:inset 0 0 0 1px var(--b-bd2);border-radius:2px;overflow:hidden;font-family:var(--b-mono);font-size:7.5px;line-height:1.35;color:var(--b-t2);display:flex;flex-direction:column}
.vb-isow .xif-hd{flex-shrink:0;display:flex;align-items:center;gap:3px;height:11px;padding:0 5px;background:var(--b-s3);color:var(--b-t3);font-size:6.5px}.vb-isow .xif-hd i{width:4px;height:4px;border-radius:50%;background:var(--b-bd2);flex-shrink:0}.vb-isow .xif-hd i:nth-child(1){background:var(--b-ac4)}.vb-isow .xif-hd i:nth-child(2){background:var(--b-ac3)}.vb-isow .xif-hd i:nth-child(3){background:var(--b-ac2)}.vb-isow .xif-hd span{margin-left:3px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.vb-isow .xif-bd{flex:1;min-height:0;padding:4px 6px;display:flex;flex-direction:column;gap:2px;overflow:hidden}.vb-isow .xif-ln{display:flex;gap:6px;white-space:nowrap;overflow:hidden;flex-shrink:0}.vb-isow .xif-ln .v{margin-left:auto;color:var(--b-t3);flex-shrink:0}.vb-isow .xif-ln .v:empty{display:none}
.vb-isow .xif-ln.p{color:var(--b-ac2)}.vb-isow .xif-ln.ac{color:var(--b-ac)}.vb-isow .xif-ln.dim{color:var(--b-t3)}.vb-isow .xif-ln.ok .v{color:var(--b-ac2)}.vb-isow .xif-ln.bad .v{color:var(--b-ac4)}.vb-isow .xif-ln.warn .v{color:var(--b-ac3)}.vb-isow .xif-ln.cur{animation:vb-xifblink 1s steps(2) infinite}@keyframes vb-xifblink{to{opacity:.2}}
.vb-isow .xif-ln.h{font-family:var(--b-ui);font-size:9px;font-weight:600;color:var(--b-t1);white-space:normal}.vb-isow .xif-ln.sh{font-family:var(--b-ui);font-size:7.5px;font-weight:600;color:var(--b-t2)}
.vb-isow .xif.page,.vb-isow .xif.web,.vb-isow .xif.form{font-family:var(--b-ui);background:var(--b-surf)}.vb-isow .xif.page .xif-ln,.vb-isow .xif.web .xif-ln{white-space:normal;display:block;font-size:6.8px;line-height:1.45}
.vb-isow .xif-ln.code{font-family:var(--b-mono);color:var(--b-t1);background:var(--b-s2);padding:1px 4px;border-radius:2px;border-left:2px solid var(--b-bd2)}.vb-isow .xif-ln.run{border-left-color:var(--b-ac)}.vb-isow .xif-ln.out{font-family:var(--b-mono);color:var(--b-ac2);padding-left:6px}
.vb-isow .xif-ln.url{font-family:var(--b-mono);background:var(--b-s2);border-radius:6px;padding:1px 6px;color:var(--b-t3);font-size:6.5px;margin-bottom:2px}
.vb-isow .xif-ln.row{padding:1px 0;box-shadow:0 1px 0 var(--b-bd)}.vb-isow .xif-ln.row .k{display:flex;align-items:center;gap:4px}.vb-isow .xif-ln.row .k::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--b-t3);flex-shrink:0}.vb-isow .xif-ln.ok .k::before{background:var(--b-ac2)}.vb-isow .xif-ln.bad .k::before{background:var(--b-ac4)}.vb-isow .xif-ln.warn .k::before{background:var(--b-ac3)}
.vb-isow .xif-ln.field{flex-direction:column;gap:1px;padding:1px 0}.vb-isow .xif-ln.field .k{font-family:var(--b-ui);font-size:6.5px;color:var(--b-t3)}.vb-isow .xif-ln.field .v{margin:0;height:9px;border-radius:2px;background:var(--b-s2);box-shadow:inset 0 0 0 1px var(--b-bd);color:var(--b-t1);padding:0 3px;font-size:6.5px;line-height:9px;overflow:hidden}.vb-isow .xif-ln.field.lit .v{box-shadow:inset 0 0 0 1px var(--b-ac)}
.vb-isow .xif-ln.btn{align-self:flex-end;background:var(--b-ac);color:#0b0d11;border-radius:3px;padding:1px 6px;font-family:var(--b-ui);font-weight:600;font-size:6.5px}
.vb-isow .xif-ln.msg{background:var(--b-s2);border-radius:5px;padding:2px 5px;white-space:normal;max-width:84%;font-family:var(--b-ui);font-size:6.8px}.vb-isow .xif-ln.me{align-self:flex-end;background:color-mix(in srgb,var(--b-ac) 22%,var(--b-s2))}
.vb-isow .xif-bars{display:flex;align-items:flex-end;gap:3px;height:22px;margin-top:auto}.vb-isow .xif-bars i{flex:1;background:var(--b-ac);border-radius:1px 1px 0 0;opacity:.85}.vb-isow .xif-bars:empty{display:none}.vb-isow .xif.chart .xif-bars{height:64px}
.vb-isow .xif.term{background:#0b0d11;color:#b9c2d0}.vb-isow .xif.term .xif-hd{background:#151920}
.vb-isow .xif-tiles,.vb-isow .xif-img{display:none}.vb-isow .xif.dash .xif-tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;flex:1}.vb-isow .xif-tiles i{background:var(--b-s2);border-radius:2px;box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-isow .xif-tiles i:nth-child(1){grid-column:span 2;background:color-mix(in srgb,var(--b-ac) 18%,var(--b-s2))}.vb-isow .xif-tiles i:nth-child(4){background:color-mix(in srgb,var(--b-ac2) 18%,var(--b-s2))}
.vb-isow .xif.img .xif-img{display:block;flex:1;border-radius:2px;background:linear-gradient(135deg,color-mix(in srgb,var(--b-ac5) 45%,var(--b-s2)),color-mix(in srgb,var(--b-ac) 30%,var(--b-s2)) 60%,var(--b-s2))}.vb-isow .xif-ln.bar{color:var(--b-ac);font-family:var(--b-mono)}
/* orbit — things circling a core */
.vb-orbit .orbw{position:relative;width:100%}.vb-orbit .ocore{position:absolute;left:50%;top:50%;width:30px;height:30px;margin:-15px 0 0 -15px;border-radius:50%;background:var(--b-ac);box-shadow:0 0 26px 6px color-mix(in srgb,var(--b-ac) 40%,transparent)}.vb-orbit .ocore em{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:10px;font-weight:700;color:var(--b-on);font-style:normal}
.vb-orbit .oring{position:absolute;left:50%;top:50%;border-radius:50%;aspect-ratio:1;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--b-ac) 20%,transparent)}.vb-orbit .ospin{position:absolute;inset:0;animation:vb-sweep linear infinite}
.vb-orbit .od{position:absolute;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 8px 1px color-mix(in srgb,var(--b-ac) 45%,transparent)}.vb-orbit .od em{position:absolute;left:50%;top:calc(100% + 3px);transform:translateX(-50%);font-style:normal;font-family:var(--b-mono);font-size:8.5px;color:var(--b-t2);white-space:nowrap;text-shadow:0 1px 3px var(--bg,#0e0f12)}
.vb-orbit .obig{position:absolute;left:0;top:0;display:flex;align-items:baseline;gap:6px}.vb-orbit .obig b{font-family:var(--b-mono);font-size:20px;font-weight:700;letter-spacing:-.03em;line-height:1}.vb-orbit .obig span{font-size:9.5px;color:var(--b-t3)}
/* turbine — blades at the rate, the blur behind is the last minute */
.vb-turbine .fanw{position:relative;display:flex;align-items:center;gap:10px;width:100%}.vb-turbine .fanw svg{height:100%;flex:0 0 auto;aspect-ratio:1;max-width:48%}.vb-turbine .fanspin{transform-origin:60px 60px;animation:vb-sweep linear infinite}.vb-turbine .bl{fill:var(--b-ac)}.vb-turbine .bl2{fill:color-mix(in srgb,var(--b-ac) 72%,var(--b-s3))}.vb-turbine .fanblur{transform-origin:60px 60px;animation:vb-sweep linear infinite;opacity:.28}
.vb-turbine .fanr{display:flex;flex-direction:column;gap:7px;min-width:0;flex:1}.vb-turbine .fanr .big{font-family:var(--b-mono);font-size:22px;font-weight:700;letter-spacing:-.035em;line-height:1}.vb-turbine .fanr .kv{display:flex;justify-content:space-between;gap:10px;font-size:10px;color:var(--b-t2)}.vb-turbine .fanr .kv b{font-family:var(--b-mono);color:var(--b-t1);font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* scope — a trace with a sweep head and phosphor behind it */
.vb-scope .scw{position:relative;overflow:hidden;border-radius:var(--b-r);background:color-mix(in srgb,var(--b-ac) 5%,var(--b-s2));width:100%}.vb-scope .scw svg{position:absolute;inset:0;width:100%;height:100%}.vb-scope .gl{stroke:var(--b-bd);stroke-width:1}
.vb-scope .beam{position:absolute;top:0;bottom:0;width:64px;pointer-events:none;background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--b-ac2) 26%,transparent));animation:vb-scan 3.6s linear infinite}@keyframes vb-scan{from{left:-64px}to{left:100%}}
.vb-scope .rd{position:absolute;right:8px;top:7px;display:flex;gap:10px;font-family:var(--b-mono);font-size:9.5px;color:var(--b-t2)}.vb-scope .rd b{color:var(--b-t1);font-weight:400}
/* pulse — a ring per event, expanding and fading */
.vb-pulse .plw{position:relative;overflow:hidden;width:100%}.vb-pulse .pr{position:absolute;left:50%;top:50%;width:9%;aspect-ratio:1;margin:-4.5% 0 0 -4.5%;border-radius:50%;border:1.5px solid currentColor;animation:vb-pring 3.2s cubic-bezier(.15,.6,.35,1) infinite;opacity:0}@keyframes vb-pring{0%{transform:scale(.4);opacity:.85}70%{opacity:.22}100%{transform:scale(7.6);opacity:0}}
.vb-pulse .phub{position:absolute;left:50%;top:50%;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;background:var(--b-ac);box-shadow:0 0 18px 4px color-mix(in srgb,var(--b-ac) 45%,transparent)}.vb-pulse .pk{position:absolute;display:flex;align-items:center;gap:5px;font-size:9.5px;color:var(--b-t2)}.vb-pulse .pk i{width:7px;height:7px;border-radius:50%}
.vb-pulse .prate{position:absolute;left:0;top:0;font-family:var(--b-mono);font-size:24px;font-weight:700;letter-spacing:-.03em;color:var(--b-t1)}.vb-pulse .prate small{font-size:9.5px;font-weight:400;color:var(--b-t3);margin-left:5px}
/* split-flap — a value that flips into place */
.vb-split-flap .flapw{display:flex;flex-direction:column;gap:9px;justify-content:center;width:100%}.vb-split-flap .flap{display:flex;gap:2px;flex-wrap:wrap}.vb-split-flap .flap span{width:17px;height:26px;border-radius:3px;background:var(--b-s3);color:var(--b-t1);display:flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:13px;position:relative;overflow:hidden;box-shadow:inset 0 0 0 1px var(--b-bd);animation:vb-flip 7s ease-in-out infinite;animation-delay:calc(var(--i,0) * 55ms);transform-origin:50% 50%}
.vb-split-flap .flap span::after{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--bg,#0e0f12);opacity:.55}.vb-split-flap .flap span.sp{background:transparent;box-shadow:none}.vb-split-flap .flap span.hi{background:color-mix(in srgb,var(--b-ac) 28%,var(--b-s3))}@keyframes vb-flip{0%,92%,100%{transform:rotateX(0)}94%{transform:rotateX(-88deg)}96%{transform:rotateX(0)}}.vb-split-flap .flapl{display:flex;gap:9px;font-size:9.5px;color:var(--b-t3)}
/* comet — a day as a ring, the head at now */
.vb-comet .cmw{position:relative;width:100%}.vb-comet .cmk,.vb-comet .cmr{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);height:88%;aspect-ratio:1}.vb-comet .cmr{border-radius:50%}
.vb-comet .cmr i{position:absolute;inset:0;border-radius:50%;display:block;background:conic-gradient(from 0deg,transparent 0 62%,color-mix(in srgb,var(--b-ac) 30%,transparent) 88%,var(--b-ac) 100%);-webkit-mask:radial-gradient(circle,transparent 0 calc(50% - 9px),#000 calc(50% - 8px) 50%,transparent 50%);mask:radial-gradient(circle,transparent 0 calc(50% - 9px),#000 calc(50% - 8px) 50%,transparent 50%);animation:vb-sweep 24s linear infinite}
.vb-comet .cmt{position:absolute;border-radius:50%;transform:translate(-50%,-50%)}.vb-comet .cmc{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center}.vb-comet .cmc b{display:block;font-family:var(--b-mono);font-size:26px;font-weight:700;letter-spacing:-.03em;line-height:1}.vb-comet .cmc span{font-size:9px;color:var(--b-t3)}.vb-comet .cmh{position:absolute;font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3);transform:translate(-50%,-50%)}
/* the bridge variables reach the faces drawn outside a .vw-b too - a composite's slots, a chip row, the donut - so a
   status colour (var(--b-ac2) ...) is a colour there as well: the yes/no dots and a status donut drew nothing */
:host,.vw-comp,.vw-chips,.vw-donut,.vw-l,.vw-xl{--b-ac:var(--ac,var(--acc,#6ea8d8));--b-ac2:var(--ac2,var(--acc2,#5ec9a0));--b-ac3:var(--ac3,var(--acc3,#e09a55));--b-ac4:var(--ac4,var(--err,#e06060));--b-ac5:var(--ac5,#a78bfa);--b-s2:var(--s2,var(--bg2,#1a1d24));--b-s3:var(--s3,var(--bg3,#222630));--b-surf:var(--surf,var(--bg1,#15181e));--b-surf2:var(--surf2,var(--bg2,#1b1f27));--b-surf3:var(--surf3,var(--bg3,#242934));--b-t1:var(--t1,var(--text,#d4dae4));--b-t2:var(--t2,var(--dim2,#8a92a0));--b-t3:var(--t3,var(--dim,#6b7280));--b-bd:var(--bd,var(--border,rgba(255,255,255,.07)));--b-bd2:var(--bd2,var(--border2,rgba(255,255,255,.14)));--b-on:var(--on-ac,#0b1119);--b-dv1:var(--dv1,#866ec5);--b-dv2:var(--dv2,#54a863);--b-dv3:var(--dv3,#3585c9);--b-dv4:var(--dv4,#bb881a);--b-dv5:var(--dv5,#b95c88);--b-dv6:var(--dv6,#00aba4);--b-dv7:var(--dv7,#bd6533);--b-mono:var(--f-mono,var(--mono,ui-monospace,Menlo,monospace));--b-ui:var(--f-ui,var(--sans,system-ui,sans-serif));--b-r:var(--r-sm,6px);--b-pill:var(--r-pill,999px);}
.vw-b{--b-ac:var(--ac,var(--acc,#6ea8d8));--b-ac2:var(--ac2,var(--acc2,#5ec9a0));--b-ac3:var(--ac3,var(--acc3,#e09a55));--b-ac4:var(--ac4,var(--err,#e06060));--b-ac5:var(--ac5,#a78bfa);--b-s2:var(--s2,var(--bg2,#1a1d24));--b-s3:var(--s3,var(--bg3,#222630));--b-surf:var(--surf,var(--bg1,#15181e));--b-surf2:var(--surf2,var(--bg2,#1b1f27));--b-surf3:var(--surf3,var(--bg3,#242934));--b-t1:var(--t1,var(--text,#d4dae4));--b-t2:var(--t2,var(--dim2,#8a92a0));--b-t3:var(--t3,var(--dim,#6b7280));--b-bd:var(--bd,var(--border,rgba(255,255,255,.07)));--b-bd2:var(--bd2,var(--border2,rgba(255,255,255,.14)));--b-on:var(--on-ac,#0b1119);--b-dv1:var(--dv1,#866ec5);--b-dv2:var(--dv2,#54a863);--b-dv3:var(--dv3,#3585c9);--b-dv4:var(--dv4,#bb881a);--b-dv5:var(--dv5,#b95c88);--b-dv6:var(--dv6,#00aba4);--b-dv7:var(--dv7,#bd6533);--b-mono:var(--f-mono,var(--mono,ui-monospace,Menlo,monospace));--b-ui:var(--f-ui,var(--sans,system-ui,sans-serif));--b-r:var(--r-sm,6px);--b-pill:var(--r-pill,999px);
  width:100%;min-width:0;min-height:0;max-height:100%;overflow:hidden;font-family:var(--b-ui);color:var(--b-t1);font-size:10px;display:flex;flex-direction:column;gap:6px;font-variant-numeric:tabular-nums;box-sizing:border-box}
.vw-b *,.vw-b *::before,.vw-b *::after{box-sizing:border-box}
.vw-b button{font:inherit;color:inherit;background:none;border:none;cursor:pointer;padding:0}
.vw-b.vb-center{align-items:center;justify-content:center}
.vb-lbl{font-size:9.5px;color:var(--b-t2);line-height:1.35}.vb-lbl.up,.vb-lbl .up,.vw-b .up{color:var(--b-ac2)}.vb-lbl.dn,.vb-lbl .dn,.vw-b .dn{color:var(--b-ac4)}
.vb-row{display:flex;align-items:center;gap:8px;width:100%}
.vb-chart{flex:none;min-height:56px;position:relative;width:100%}.vb-chart svg{display:block;width:100%;height:100%;min-height:56px;overflow:visible}.vb-chart.rel{min-height:64px}
.vb-spk{width:100%;height:18px;display:block;overflow:visible}
.vb-hero{display:flex;align-items:baseline;gap:7px}.vb-hero b{font-family:var(--b-mono);font-size:clamp(16px, min(60cqh, 12cqw), 30px);font-weight:700;letter-spacing:-.035em;line-height:1}.vb-hero .u{font-family:var(--b-mono);font-size:13px;color:var(--b-t2)}
.vb-lg{display:flex;flex-direction:column;gap:4px;min-width:0}.vb-lg span{display:flex;align-items:center;gap:7px;font-size:10px;color:var(--b-t2);white-space:nowrap;overflow:hidden}.vb-lg i{width:8px;height:8px;border-radius:2px;flex-shrink:0}.vb-lg b{margin-left:auto;font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);font-weight:400}.vb-lg.row{flex-direction:row;flex-wrap:wrap;gap:4px 12px}
.vb-lgr{display:flex;align-items:center;gap:12px;font-size:9.5px;color:var(--b-t2)}.vb-lgr span{display:inline-flex;align-items:center;gap:6px}.vb-lgr i{width:8px;height:8px;border-radius:2px}
.vb-rw{display:flex;align-items:center;gap:8px;font-size:10px}.vb-rw .n{width:clamp(64px,24%,170px);flex-shrink:0;color:var(--b-t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-rw .tr{flex:1;height:7px;border-radius:4px;background:var(--b-s3);overflow:hidden;position:relative}.vb-rw .tr i{display:block;height:100%;border-radius:4px}.vb-rw .tr em{position:absolute;top:-2px;width:2px;height:11px;background:var(--b-t1);transform:translateX(-50%)}.vb-rw .v{min-width:38px;white-space:nowrap;text-align:right;font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);flex-shrink:0}
/* the standard set */
.vb-fc{flex:1;min-height:0;border-radius:var(--b-r);background:var(--b-surf2);padding:9px 11px;display:flex;flex-direction:column;gap:4px;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14));animation:vb-fcin .35s ease}@keyframes vb-fcin{from{opacity:0;transform:translateX(10px)}}
.vb-fc .k{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);display:flex;gap:6px;align-items:center}.vb-fc .k i{width:6px;height:6px;border-radius:50%}.vb-fc .h{font-size:12px;font-weight:600;color:var(--b-t1);line-height:1.35}.vb-fc .b{font-size:10px;color:var(--b-t2);line-height:1.45;overflow:hidden}.vb-fc .m{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3);display:flex;gap:8px}
.vb-fn,.vb-carn{display:flex;align-items:center;gap:5px}.vb-fn button,.vb-carn button{width:22px;height:18px;border-radius:var(--b-r);background:var(--b-surf2);color:var(--b-t2);font-size:11px}.vb-fn i,.vb-carn i{width:6px;height:6px;border-radius:50%;background:var(--b-s3);cursor:pointer}.vb-fn i.on,.vb-carn i.on{background:var(--b-ac)}.vb-fn span{margin-left:auto;font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3)}.vb-carn .vb-lbl{margin-left:8px}
.vb-fr{display:grid;grid-template-columns:18px 1fr 44px 52px;gap:7px;align-items:center;height:22px;font-size:10px;border-bottom:1px solid var(--b-bd)}.vb-fr.h{color:var(--b-t3);font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;height:18px}.vb-fr .ic{width:14px;height:16px;border-radius:2px;font-family:var(--b-mono);font-size:6.5px;font-weight:700;color:#0e0f12;display:flex;align-items:flex-end;justify-content:center;padding-bottom:1px}.vb-fr .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-fr .n small{color:var(--b-t3);margin-left:5px;font-size:9px}.vb-fr .m{font-family:var(--b-mono);font-size:9px;color:var(--b-t3);text-align:right}
.vb-dgr{display:grid;gap:6px;align-items:center;height:21px;font-size:9.5px;border-bottom:1px solid var(--b-bd)}.vb-dgr.h{height:20px}.vb-dgr.h button{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);text-align:left;display:flex;gap:3px;align-items:center;white-space:nowrap}.vb-dgr.h button.on{color:var(--b-t1)}.vb-dgr:nth-child(odd):not(.h){background:color-mix(in srgb,var(--b-surf2) 60%,transparent)}.vb-dgr .c{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:4px}.vb-dgr .c.num{font-family:var(--b-mono);color:var(--b-t2);text-align:right;padding-left:0}.vb-dgr .c.dn{color:var(--b-ac4)}.vb-r2w{display:flex;flex-direction:column;gap:1px}.vb-dgr[data-r2]{display:flex;align-items:center;gap:8px;height:auto;min-height:23px;padding:3px 5px;border-bottom:1px solid var(--b-bd);border-radius:4px;font-size:11px;cursor:pointer}.vb-dgr[data-r2]:hover{background:var(--b-surf2)}.vb-dgr[data-r2] > .st{flex:none;width:8px;height:8px;border-radius:50%}.vb-dgr[data-r2] > .st.none{background:var(--b-bd2)}.vb-dgr[data-r2] .nm{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}.vb-dgr[data-r2] .nm b{font-weight:500;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-dgr[data-r2] .nm small{font-size:9.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-dgr[data-r2] .nm small em{font-style:normal;margin-left:8px;color:var(--b-t2)}.vb-dgr[data-r2] .nm small em:first-child{margin-left:0}.vb-dgr[data-r2] .pill{flex:none;font-size:9.5px;line-height:1.5;padding:0 7px;border-radius:999px;color:var(--c);background:color-mix(in srgb,var(--c) 14%,transparent);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--c) 35%,transparent);white-space:nowrap}.vb-dgr[data-r2] .vv{flex:none;position:relative;min-width:52px;max-width:42%;text-align:right;font-family:var(--b-mono);font-size:10.5px;color:var(--b-t1);padding:1px 0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-dgr[data-r2] .vv .bar{position:absolute;right:0;bottom:0;height:2px;border-radius:2px;background:var(--b-ac);opacity:.75}.vb-r2s{display:flex;flex-direction:column;gap:5px;margin:0 0 6px}.vb-r2s .bar{display:flex;height:6px;border-radius:3px;overflow:hidden;gap:1px}.vb-r2s .bar i{display:block;min-width:3px}.vb-r2s .lg{display:flex;flex-wrap:wrap;gap:4px}.vb-r2s .lg button{display:inline-flex;align-items:center;gap:5px;font-size:10px;padding:1px 7px;border-radius:999px;color:var(--b-t2);background:var(--b-surf2);box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-r2s .lg button.on{color:var(--b-t1);box-shadow:inset 0 0 0 1px var(--b-ac)}.vb-r2s .lg i{width:7px;height:7px;border-radius:50%}.vb-r2s .lg b{font-family:var(--b-mono);font-weight:500;color:var(--b-t1)}.vb-r2m{cursor:pointer;margin-top:4px;padding-left:5px}.vb-r2m:hover{color:var(--b-ac)}
.vb-dgf{display:flex;align-items:center;gap:6px;font-size:8.5px;color:var(--b-t3);padding-top:4px;margin-top:auto}
.vb-dgr .c .st{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px;vertical-align:middle}
.vb-tsearch{display:flex;align-items:center;gap:6px;height:24px;padding:0 8px;border-radius:var(--b-r);background:var(--b-s2);color:var(--b-t3);font-size:10px;box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-tsearch input{flex:1;border:none;background:transparent;color:var(--b-t1);font:inherit;font-size:10px;min-width:0;outline:none}
.vb-cards{display:grid;gap:6px;flex:none;align-content:start}.vb-card{position:relative;background:var(--b-surf2);border-radius:var(--b-r);padding:7px 9px 7px 18px;display:flex;flex-direction:column;gap:2px;min-width:0;box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}.vb-card i{position:absolute;left:7px;top:10px;width:6px;height:6px;border-radius:50%}.vb-card b{font-size:11px;font-weight:600;color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-card span{font-size:9.5px;color:var(--b-t1);font-family:var(--b-mono);display:flex;gap:6px;white-space:nowrap;overflow:hidden}.vb-card small{color:var(--b-t3);font-family:var(--b-ui)}
.vb-tf{gap:2px}.vb-dgf button{width:18px;height:16px;border-radius:4px;background:var(--b-surf2);color:var(--b-t2)}
.vb-gal{flex:none;min-height:110px;display:grid;grid-template-columns:repeat(4,1fr);grid-auto-rows:1fr;gap:5px}.vb-gal .g{border-radius:var(--b-r);position:relative;overflow:hidden;min-height:44px;cursor:pointer;transition:transform .15s ease}.vb-gal .g:hover{transform:scale(1.04)}.vb-gal .g span{position:absolute;left:0;right:0;bottom:0;padding:3px 6px;font-size:8px;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,.6));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-gal .g b{position:absolute;right:4px;top:3px;font-family:var(--b-mono);font-size:7.5px;color:#fff;opacity:.8}
.vb-trm{flex:1;min-height:0;border-radius:var(--b-r);background:var(--b-s2);padding:7px 9px;font-family:var(--b-mono);font-size:9.5px;line-height:1.6;color:var(--b-t2);white-space:pre-wrap;overflow:hidden;box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-trm b{color:var(--b-ac2);font-weight:500}.vb-trm em{color:var(--b-ac3);font-style:normal}.vb-trm .car{display:inline-block;width:6px;height:11px;background:var(--b-t1);vertical-align:-2px;animation:vb-tcur 1s steps(1) infinite}@keyframes vb-tcur{0%,50%{opacity:1}51%,100%{opacity:0}}
.vb-globe{display:flex;gap:10px;flex:1;min-height:0;height:100%}.vb-globe .gw{flex:1;min-width:0;position:relative;display:flex;align-items:center;justify-content:center}.vb-globe .gw svg{width:100%;height:100%;max-height:100%;overflow:visible;position:relative;z-index:1}
.vb-globe .gl{stroke:var(--b-bd2);stroke-width:.6;fill:none}.vb-globe .rim{fill:none;stroke:color-mix(in srgb,var(--b-ac) 45%,transparent);stroke-width:1}.vb-globe .term{fill:rgba(0,0,0,.28)}.vb-globe .pin{stroke:var(--b-s2);stroke-width:1}.vb-globe .pin.p{animation:vb-gpp 2.4s ease-in-out infinite}@keyframes vb-gpp{0%,100%{r:4}50%{r:6}}
.vb-globe .pl{font-family:var(--b-mono);font-size:7.5px;fill:var(--b-t2)}.vb-globe .pn{font-family:var(--b-mono);font-size:7px;font-weight:700;fill:var(--b-s2)}.vb-globe .arc{fill:none;stroke-width:1;stroke-dasharray:3 3;animation:vb-arcm 1.4s linear infinite}@keyframes vb-arcm{to{stroke-dashoffset:-12}}
.vb-globe .scan{transform-box:fill-box;transform-origin:50% 50%;animation:vb-scan 6s linear infinite}@keyframes vb-scan{to{transform:rotate(360deg)}}.vb-globe .sw{stroke:none;fill:var(--b-ac2)}
.vb-globe .side{width:40%;max-width:160px;flex:0 0 auto;display:flex;flex-direction:column;gap:3px;min-height:0;overflow:hidden}.vb-globe .r{display:grid;grid-template-columns:14px 1fr;gap:6px;align-items:start;font-size:9.5px;color:var(--b-t2);line-height:1.3;padding:3px 0;border-bottom:1px solid var(--b-bd)}.vb-globe .r b{color:var(--b-t1);font-weight:500;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vb-globe .r i{width:12px;height:12px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:7.5px;font-weight:700;color:var(--b-s2);margin-top:1px}.vb-globe .r small{color:var(--b-t3);font-family:var(--b-mono);font-size:8px}
.vb-globe .nav{display:flex;align-items:center;gap:5px;margin-top:auto;padding-top:3px}.vb-globe .nav button{width:22px;height:18px;border-radius:5px;background:var(--b-surf2);color:var(--b-t2);font:inherit;font-size:11px;border:0;cursor:pointer;padding:0}.vb-globe .nav button:hover{color:var(--b-t1)}.vb-globe .nav span{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3)}
.vb-globe .plinth{position:absolute;left:50%;bottom:3%;width:74%;aspect-ratio:2/1;transform:translateX(-50%);background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02));border-radius:50%;box-shadow:0 0 0 1px var(--b-bd2),0 18px 30px -20px #000;z-index:0}
[data-motion="0"] .vb-globe .arc,[data-motion="0"] .vb-globe .scan,[data-motion="0"] .vb-globe .pin.p{animation:none}
.vb-trmh{display:flex;gap:6px;align-items:center;font-size:8.5px;color:var(--b-t3);font-family:var(--b-mono)}.vb-trmh i{width:7px;height:7px;border-radius:50%;background:var(--b-ac2)}
.vb-ag{display:grid;grid-template-columns:38px 3px 1fr auto;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid var(--b-bd);font-size:10px}.vb-ag .t{font-family:var(--b-mono);font-size:9px;color:var(--b-t3)}.vb-ag i{width:3px;height:22px;border-radius:2px}.vb-ag .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ag .n small{display:block;color:var(--b-t3);font-size:8.5px}.vb-ag .w{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3)}.vb-ag.now{background:color-mix(in srgb,var(--b-ac) 10%,transparent);border-radius:var(--b-r);padding:4px 6px;margin:0 -6px}.vb-ag.now .t{color:var(--b-ac)}
.vb-pr{display:grid;grid-template-columns:24px 1fr auto;gap:8px;align-items:center;height:28px;font-size:10px}.vb-pr .av{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:8.5px;font-weight:700;color:#0e0f12;position:relative}.vb-pr .av i{position:absolute;right:-1px;bottom:-1px;width:7px;height:7px;border-radius:50%;box-shadow:0 0 0 2px var(--b-surf)}.vb-pr .n{color:var(--b-t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-pr .n small{display:block;color:var(--b-t3);font-size:8.5px}.vb-pr .s{font-family:var(--b-mono);font-size:8.5px;color:var(--b-t3);white-space:nowrap}
.vb-ql{flex:1;min-height:0;display:grid;grid-template-columns:repeat(3,1fr);grid-auto-rows:1fr;gap:6px}.vb-ql button,.vb-ql a{border-radius:var(--b-r);background:var(--b-surf2);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;font-size:9px;color:var(--b-t2);box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14));min-height:36px;text-decoration:none;padding:4px}.vb-ql button:hover,.vb-ql a:hover{color:var(--b-t1);background:color-mix(in srgb,var(--b-ac) 12%,var(--b-surf2))}.vb-ql b{font-family:var(--b-mono);font-size:13px;color:var(--b-t1)}
.vb-ann{flex:1;min-height:0;border-radius:var(--b-r);padding:10px 12px;display:flex;flex-direction:column;gap:5px;background:color-mix(in srgb,var(--pc) 10%,var(--b-surf2));box-shadow:inset 3px 0 0 0 var(--pc)}.vb-ann .k{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--pc);font-weight:600}.vb-ann .h{font-size:13px;font-weight:600;color:var(--b-t1);line-height:1.3}.vb-ann .b{font-size:10px;color:var(--b-t2);line-height:1.45;overflow:hidden}.vb-ann .a{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.vb-ann .a button{height:21px;padding:0 9px;border-radius:var(--b-pill);background:var(--b-surf);font-size:9.5px;color:var(--b-t1);box-shadow:var(--elev-lo,0 1px 2px rgba(0,0,0,.14))}.vb-ann .a button.pri{background:var(--pc);color:#1a1408;font-weight:600}.vb-ann .a small{margin-left:auto;color:var(--b-t3);font-size:8.5px}
.vb-kb{flex:1;min-height:0;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.vb-kb .kc{background:var(--b-surf2);border-radius:var(--b-r);padding:6px;display:flex;flex-direction:column;gap:4px;min-height:0;min-width:0;overflow:hidden}.vb-kb .kh{font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--b-t3);display:flex;gap:5px}.vb-kb .kh b{margin-left:auto;font-family:var(--b-mono);font-weight:400}.vb-kb .kt{background:var(--b-surf);border-radius:4px;padding:4px 6px;font-size:9px;color:var(--b-t1);line-height:1.3;box-shadow:inset 2px 0 0 0 var(--kc);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-kb .kt small{display:block;color:var(--b-t3);font-size:8px}
/* levels */
.vb-gg{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px}.vb-gg b{font-family:var(--b-mono);font-size:12px}.vb-gg .vb-lbl{font-size:9px}
.vb-dial{position:relative;flex-shrink:0;display:block}.vb-dial svg{width:100%;height:100%;transform:rotate(-90deg);display:block}.vb-dial > span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:15px;font-weight:700}
.vb-bar2{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--b-s3);width:100%}.vb-bar2 i{display:block;height:100%}
.vb-colbars{display:flex;align-items:flex-end;gap:2px;height:100%;min-height:56px;width:100%}.vb-colbars.gap{gap:3px}.vb-colbars i{flex:1;border-radius:2px 2px 0 0;display:block;min-height:1px}.vb-cols{flex:none;min-height:24px;display:flex}
.vb-seg7{display:flex;gap:3px;justify-content:center;align-items:baseline;padding:2px 0;flex-wrap:nowrap;max-width:100%;overflow:hidden}.vb-seg7 span{position:relative;font-family:var(--b-mono);font-size:clamp(13px, min(68cqh, 15cqw), 34px);font-weight:700;line-height:1;letter-spacing:-.02em}.vb-seg7 span.p{font-size:clamp(12px, 44cqh, 22px);color:var(--b-t3)}.vb-seg7 span.u{font-size:clamp(9px, 26cqh, 13px)}.vb-seg7 span::before{content:attr(data-g);position:absolute;left:0;top:0;opacity:0;pointer-events:none}
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
.vb-bigs{display:grid;grid-template-columns:1fr 1fr;gap:8px;flex:1;min-height:0;align-content:space-evenly;height:100%}.vb-bigs div{display:flex;flex-direction:column;gap:1px;min-width:0;container-type:inline-size}.vb-bigs b{font-family:var(--b-mono);font-size:clamp(13px, min(30cqh, 20cqi), 30px);font-weight:700;letter-spacing:-.03em;line-height:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-bigs span{font-size:9px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-bigs.tworow{gap:4px 8px}.vb-bigs.tworow b{font-size:clamp(13px, min(22cqh, 9cqw), 24px)}
.vb-pillw{display:flex;flex-wrap:wrap;gap:5px}.vb-pillw span{height:22px;padding:0 9px;display:inline-flex;align-items:center;gap:6px;border-radius:var(--b-pill);background:var(--b-surf2);font-size:9.5px;color:var(--b-t2)}.vb-pillw i{width:6px;height:6px;border-radius:50%}.vb-pillw b{font-family:var(--b-mono);font-weight:400;color:var(--b-t1)}.vb-pillw span.more{color:var(--b-t3);background:transparent;box-shadow:inset 0 0 0 1px var(--b-bd)}.vb-pillw span{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* parts */
.vb-funnel{display:flex;flex-direction:column;gap:4px;flex:1;min-height:0;justify-content:center}.vb-funnel span{height:24px;border-radius:3px;display:flex;align-items:center;padding:0 9px;font-size:9.5px;color:var(--b-on);align-self:center;justify-content:space-between;gap:8px}.vb-funnel b{font-family:var(--b-mono);font-weight:400}
.vb-waffle{display:grid;gap:3px;flex:1;min-height:0;align-content:center}.vb-waffle i{aspect-ratio:1;border-radius:2px}
.vb-stackbar{display:flex;height:26px;border-radius:5px;overflow:hidden;gap:1px;background:var(--b-s3);width:100%}.vb-stackbar i{display:block;height:100%}
.vb-tmap{flex:none;min-height:40px;display:flex;flex-wrap:wrap;gap:2px;align-content:stretch}.vb-tmap span{border-radius:3px;display:flex;align-items:flex-end;padding:4px 5px;font-family:var(--b-mono);font-size:8px;color:var(--b-on);overflow:hidden;white-space:nowrap}
/* matrices and calendars */
.vb-heatrow{display:grid;grid-template-columns:46px 1fr 32px;align-items:center;gap:8px}.vb-heatrow.nt{grid-template-columns:46px 1fr}.vb-heatfit{gap:3px;justify-content:center}.vb-heatrow.hd .vb-heat b{font-family:var(--b-mono);font-size:8px;font-weight:400;color:var(--b-t3);text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-heatfit .vb-heat i{display:flex;align-items:center;justify-content:center;font-family:var(--b-mono);font-size:8.5px;font-style:normal;color:var(--b-t1)}.vb-heatfit .vb-lbl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-heatrow .v{font-family:var(--b-mono);font-size:9px;text-align:right}.vb-heat{display:grid;gap:2px;align-content:center}.vb-heat i{aspect-ratio:1;border-radius:2px;display:block}
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
.vb-log{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column;gap:1px;font-family:var(--b-mono);font-size:9px;line-height:1.55}.vb-log span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-log > span{flex:none}.vb-log .t{color:var(--b-t3)}.vb-log .k{opacity:.8}
.vb-lane{display:flex;gap:6px;flex:1;min-height:0;align-items:stretch;overflow:hidden}.vb-lane div{flex:1;min-width:0;background:var(--b-surf2);border-radius:var(--b-r);padding:6px 7px;display:flex;flex-direction:column;gap:2px;box-shadow:inset 2px 0 0 var(--lc)}.vb-lane b{font-size:9px;color:var(--lc);text-transform:uppercase;letter-spacing:.08em}.vb-lane em{font-style:normal;font-size:9.5px;color:var(--b-t1);line-height:1.3;overflow:hidden}.vb-lane i{font-style:normal;font-family:var(--b-mono);font-size:8px;color:var(--b-t3);margin-top:auto}
/* graphs */
.vb-topo{flex:none;min-height:0;position:relative;width:100%}.vb-topo .tn{position:absolute;transform:translate(-50%,-50%);border-radius:50%}.vb-topo .tn.hollow{background:transparent!important;box-shadow:inset 0 0 0 1.5px var(--c)}.vb-topo .tn:not(.hollow){background:var(--c)}.vb-topo .te{position:absolute;height:1px;transform-origin:0 50%;background:var(--b-bd2)}.vb-topo .fe{position:absolute;transform-origin:0 50%;border-radius:2px;background:var(--b-ac);opacity:.5}.vb-topo .fn{position:absolute;transform:translate(-50%,-50%);border-radius:3px;padding:2px 6px;font-size:8.5px;background:var(--b-surf2);white-space:nowrap}
/* the composites */
.vb-nstat{display:flex;align-items:center;gap:7px;font-size:10px;color:var(--b-t2)}.vb-nstat i{width:8px;height:8px;border-radius:50%}
.vb-ncard,.vb-glance{display:grid;grid-template-columns:1fr 1fr;gap:6px;flex:1;min-height:0;align-content:center}.vb-ncard div,.vb-glance div{background:var(--b-surf2);border-radius:var(--b-r);padding:6px 8px;display:flex;flex-direction:column;gap:2px;min-width:0}.vb-ncard b,.vb-glance b{font-family:var(--b-mono);font-size:15px;font-weight:700;line-height:1}.vb-ncard b small{font-size:9px;font-weight:400;color:var(--b-t3);margin-left:1px}.vb-ncard span,.vb-glance span{font-size:8.5px;color:var(--b-t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vb-ncard .bar{height:4px;border-radius:2px;background:var(--b-s3);margin-top:3px;overflow:hidden}.vb-ncard .bar i{display:block;height:100%}.vb-glance .vb-spk{height:16px;margin-top:3px}
.vb-cmpr{display:grid;grid-template-columns:1fr 70px 1fr;gap:8px;align-items:center;font-size:10px}.vb-cmpr .n{grid-column:2;text-align:center;color:var(--b-t2);order:2;white-space:nowrap;overflow:hidden}.vb-cmpr .side{display:flex;align-items:center;gap:6px;height:12px}.vb-cmpr .side.l{order:1;justify-content:flex-end}.vb-cmpr .side.r{order:3}.vb-cmpr .side i{display:block;height:8px;border-radius:4px}.vb-cmpr .side b{font-family:var(--b-mono);font-size:9.5px;color:var(--b-t1);width:34px;text-align:right}.vb-cmpr .side.r b{text-align:left}
.vb-carp{flex:1;min-height:0;display:flex;align-items:center;gap:12px}.vb-carp .vb-dial{width:64px;height:64px}.vb-carp .vb-dial > span{font-size:13px}
.vb-flist{flex:1;display:flex;flex-direction:column;gap:1px;font-size:9.5px;min-width:0}.vb-flist > span{display:grid;grid-template-columns:1fr 46px 50px 36px;gap:6px;align-items:center;height:19px}.vb-flist span i{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}.vb-flist .h{color:var(--b-t3);font-size:8px;text-transform:uppercase;letter-spacing:.08em}.vb-flist .m{font-family:var(--b-mono);color:var(--b-t2);text-align:right;white-space:nowrap;overflow:hidden}
.vb-dials{width:96px;height:96px;flex-shrink:0}.vb-dials svg{width:96px;height:96px;transform:rotate(-90deg)}` + CAPOUT_CSS + FORMS3_CSS + CI_CSS + CC_CSS + PF_CSS + LINES_CSS);
  function ensureCss(root) {
    const host = root && root.head ? root.head : root;
    if (!host || !host.querySelector) return;
    if (host.querySelector('style[data-vera-widget-css]')) return;
    const st = document.createElement('style'); st.setAttribute('data-vera-widget-css', '1'); st.textContent = CSS; host.appendChild(st);
  }

  /* ── the element ──────────────────────────────────────────────────────── */
  const ELEMENT_CSS = fontScale(`:host{display:block;color:var(--text,#d8dce4);font-family:var(--sans,system-ui,sans-serif);font-size:11px;min-width:0}
:host([data-state="reading"]) .vw-sampled{opacity:.42;filter:saturate(.4)}
[data-b],[data-tip]{transition:filter .14s ease,opacity .14s ease,stroke-width .14s ease}
.vw-root.lit .vb-isow i.f[data-b]:not(.hot):not(.warm),.vw-root.lit .vw-arc:not(.hot){filter:brightness(.62) saturate(.7)}
.vb-isow i.f.hot{filter:brightness(1.55) saturate(1.25) drop-shadow(0 0 3px rgba(255,255,255,.35))}.vb-isow i.f.warm{filter:brightness(1.18)}
[data-b].hot,[data-tip].hot{filter:brightness(1.4)}.vw-arc.hot{filter:brightness(1.35) drop-shadow(0 0 2px rgba(255,255,255,.3))}
[data-ref],:host([dive-on-click]) .vw-root{cursor:pointer}
.vw-legend-col span.hot{color:var(--t1,#fff);filter:none}
:host(:not([data-entered])) .vb-isow i.f{animation:vw-rise .55s cubic-bezier(.2,.7,.2,1) both;animation-delay:calc(min(var(--i), 90) * 5ms)}
@keyframes vw-rise{from{opacity:0;transform:translateY(7px)}}
:host(:not([data-entered])) .vb-rw .tr i,:host(:not([data-entered])) .vw-therm span i{animation:vw-grow .7s cubic-bezier(.2,.7,.2,1) both;transform-origin:0 50%}
@keyframes vw-grow{from{transform:scaleX(0)}}
:host(:not([data-entered])) .vb-colbars i{animation:vw-riseup .6s cubic-bezier(.2,.7,.2,1) both;transform-origin:50% 100%}
@keyframes vw-riseup{from{transform:scaleY(0)}}
:host(:not([data-entered])) .vw-arc{animation:vw-fadein .6s ease-out both}@keyframes vw-fadein{from{opacity:0}}
.vw-root[data-motion="0"] *,.vw-root[data-motion="0"] *::before{animation:none!important;transition:none!important}
@media (prefers-reduced-motion: reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
:host([data-state="failed"]) .vw-sampled{opacity:.3;filter:grayscale(1)}
.vw-root{display:flex;flex-direction:column;gap:5px;height:100%;min-width:0}:host([bare]) .vw-root{overflow-y:auto;overflow-x:hidden;scrollbar-width:thin}
.vw-hd{display:flex;align-items:center;gap:6px;font-family:var(--mono,ui-monospace,monospace);font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim,#6b7280);font-weight:600}
.vw-hd i{width:6px;height:6px;border-radius:50%;background:var(--acc,#5a9e8f);flex-shrink:0}.vw-hd b{margin-left:auto;font-size:11px;color:var(--text,#d8dce4);font-weight:400;text-transform:none;letter-spacing:0}
.vw-body{flex:1;min-height:0;display:flex;align-items:safe center;justify-content:center;overflow:auto;font-size:10.5px;container-type:size}
.vw-cap{font-size:9px;color:var(--dim2,#8a92a0);font-family:var(--mono,ui-monospace,monospace);display:flex;gap:6px;align-items:center}.vw-cap .sp{flex:1}
.vw-acts{display:flex;gap:4px;flex-wrap:wrap}.vw-acts button{font-size:8.5px;color:var(--text,#d8dce4);background:var(--bg2,#1a1c20);border:1px solid var(--border,rgba(255,255,255,.09));border-radius:99px;padding:2px 7px;cursor:pointer;font-family:inherit}.vw-acts button:hover{color:var(--acc,#5a9e8f);border-color:var(--acc,#5a9e8f)}
.vw-read{font-size:9px;padding:2px 8px;border:1px solid var(--border,rgba(255,255,255,.09));border-radius:4px;background:var(--bg2,#1a1c20);color:var(--dim2,#8a92a0);cursor:pointer;font-family:inherit}
:host([size="xs"]) .vw-root,:host([size="s"]) .vw-root{display:inline-flex}:host([size="xs"]),:host([size="s"]){display:inline-block}
.vw-root[data-motion="0"] *,.vw-root[data-motion="0"] *::before,.vw-root[data-motion="0"] *::after{animation:none!important;transition:none!important}`);
  const ACTIONS = { dive: 'Deep dive', pin: 'Pin to canvas', ask: 'Ask Vera', ops: 'Open in Ops', print: 'Print card', mute: 'Mute' };
  // the one projection library (/ui/iso.js — window.VeraISO): loaded once by the first element that needs it, every
  // connected element redrawn when it lands (a scene drawn before it says "loading the projection…")
  const INSTANCES = new Set();
  function ensureIso(base) {
    if (typeof document === 'undefined' || !document.head || !document.head.appendChild || window.VeraISO || ensureIso.loading) return;
    if (document.querySelector && document.querySelector('script[src$="/ui/iso.js"]')) { ensureIso.loading = true; const t = setInterval(() => { if (window.VeraISO) { clearInterval(t); INSTANCES.forEach((el) => { try { el.render(); } catch (_) {} }); } }, 200); return; }
    ensureIso.loading = true; const sc = document.createElement('script'); sc.src = (base || '') + '/ui/iso.js'; sc.async = true;
    sc.onload = () => { INSTANCES.forEach((el) => { try { el.render(); } catch (_) {} }); };
    // a load that fails (a flaky connection, a certificate hiccup) is tried again, or every iso form waits forever on 'loading the projection'
    sc.onerror = () => { ensureIso.loading = false; sc.remove(); ensureIso.tries = (ensureIso.tries || 0) + 1; if (ensureIso.tries < 6) setTimeout(() => ensureIso(base), 1500 * ensureIso.tries); };
    document.head.appendChild(sc);
  }
  const REFRESH_FLOOR = 10;
  const RETRY_S = 12;
  // one capability call for the element and the surface: prod's envelope is {type:'tool_result', tool_name, content};
  // a stand-in may answer {result} or the bare object — every one is opened
  const INFLIGHT = new Map();   // one fetch per (base, name, args) at a time, shared by every element that asks
  // the read queue: at most CALL_LANES fetches in flight, first asked first served - a slow reading is never starved by
  // the fast tiles' refresh timers, and a page of fifty tiles opens without the browser's connection limit deciding
  // Six lanes (the browser's own limit per host); the SLOW readings - the ones known to take seconds (a health sweep, a
  // backup census, a topology walk) and any reading that last took over four seconds - may hold two of them at most,
  // so the fast tiles never queue behind them (a fair FIFO alone let five slow reads take every lane and the page sat
  // on its samples); a fast reading passes a waiting slow one when the slow lanes are full
  // BATCHED READS. A dashboard of fifty tiles asked on fifty connections, and the browser gives a host six, shared with
  // everything else the page loads - so the reads queued for minutes behind the shell's own requests. The readings
  // asked within a beat go to prod as ONE widget.read call (a connection each, at most 24 readings a batch); the
  // readings known to take seconds (a health sweep, a backup census, a topology walk, or one that last took over four
  // seconds) batch among themselves, so a fast reading is never held by a slow one. A server without widget.read
  // (a 404) is asked one reading at a time, as before.
  const COST = new Map(), BATCH_MAX = 24, BATCH_MS = 40, PENDING = { fast: [], slow: [] }, TIMERS = {}; let BATCH_OFF = false;
  const SLOW_NAMES = /^(estate\.health|backup\.status|fabric\.health|topology\.snapshot|mesh\.topology|perf\.scan|evolve\.errors\.list|dash\.health\.summary|evolve\.tests\.matrix|census\.landed|ci\.census)$/;   // tests.matrix collects the suite (~14 s) - batched with the Gates lens it held every tile at reading
  const isSlow = (name) => SLOW_NAMES.test(String(name || '')) || (COST.get(name) || 0) > 4000;
  const opened = (j) => (j && j.type === 'tool_result') ? j.content : (j && j.result !== undefined ? j.result : (j && j.content !== undefined ? j.content : j));
  /* A READING THAT NEVER ANSWERS must not hold its tile forever (the widget review, 2026-09-27). The browser gives a
     host six connections and the shell's own slow calls (a topology walk, a health summary, the fabric lists) hold
     them for seconds each, so a batch can sit queued; a read with no end kept its promise in INFLIGHT, and every later
     refresh of that source was handed the same dead promise - the tile drew its sample face until the page was
     reloaded. Every read now has a deadline (DEADLINE_MS, the slow lane longer); past it the read fails, the tile
     says so, and the next refresh asks again. */
  const DEADLINE_MS = { fast: 25000, slow: 60000 };
  const withDeadline = (ms, go) => { const ctl = typeof AbortController === 'function' ? new AbortController() : null; let tm = null;
    const late = new Promise((_, no) => { tm = setTimeout(() => { if (ctl) { try { ctl.abort(); } catch (_) {} } no(new Error('no answer in ' + Math.round(ms / 1000) + ' s')); }, ms); });
    return Promise.race([go(ctl ? ctl.signal : undefined), late]).finally(() => clearTimeout(tm)); };
  async function single(base, name, args) {
    return withDeadline(DEADLINE_MS[isSlow(name) ? 'slow' : 'fast'], async (signal) => {
      const r = await fetch((base || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, arguments: args || {} }), signal });
      return opened(await r.json()); });
  }
  async function flush(lane) {
    TIMERS[lane] = null; const items = PENDING[lane].splice(0, lane === 'slow' ? 4 : BATCH_MAX); if (PENDING[lane].length) TIMERS[lane] = setTimeout(() => flush(lane), 0);
    if (!items.length) return;
    const base = items[0].base, t0 = Date.now();
    const fallback = () => items.forEach((it) => single(it.base, it.name, it.args).then(it.ok, it.no));
    if (BATCH_OFF) return fallback();
    let r;
    try {
      r = await withDeadline(DEADLINE_MS[lane], (signal) => fetch((base || '') + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'widget.read', arguments: { calls: items.map((it) => ({ name: it.name, arguments: it.args || {} })) } }), signal }).then((x) => (x.status === 404 ? x : x.json().then((j) => ({ status: x.status, j })))));
    } catch (e) { items.forEach((it) => it.no(e)); return; }   // past its deadline: every reading of the batch fails now, and is asked again at its next refresh
    try {
      if (r.status === 404) { BATCH_OFF = true; return fallback(); }
      const c = opened(r.j); const res = c && Array.isArray(c.results) ? c.results : null;
      if (!res) return fallback();
      items.forEach((it, i) => { const q = res[i]; COST.set(it.name, (q && q.ms) || (Date.now() - t0)); if (q && q.ok) it.ok(q.content); else it.no(new Error((q && q.error) || 'read failed')); });
    } catch (e) { fallback(); }
  }
  function enqueue(base, name, args) { return new Promise((ok, no) => { const lane = isSlow(name) ? 'slow' : 'fast'; PENDING[lane].push({ base, name, args, ok, no }); if (!TIMERS[lane]) TIMERS[lane] = setTimeout(() => flush(lane), BATCH_MS); }); }
  async function call(base, name, args) {
    let key = ''; try { key = (base || '') + '|' + name + '|' + JSON.stringify(args || {}); } catch (_) { key = ''; }
    if (key && INFLIGHT.has(key)) return INFLIGHT.get(key);
    const p = enqueue(base, name, args);
    if (key) { INFLIGHT.set(key, p); p.then(() => setTimeout(() => INFLIGHT.delete(key), 1500), () => INFLIGHT.delete(key)); }
    return p;
  }
  const parseRefresh = (s) => { const m = String(s || '').match(/^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$/); if (!m) return 0; const n = parseFloat(m[1]); return m[2] === 'ms' ? n / 1000 : m[2] === 'm' ? n * 60 : m[2] === 'h' ? n * 3600 : n; };
  const sizeForWidth = (w) => w <= 120 ? 'xs' : w <= 220 ? 's' : w <= 380 ? 'm' : w <= 620 ? 'l' : 'xl';

  /* ══ HOVER · CLICK · MOTION · THE DEEP DIVE (the widget review, round 2) ═══════════════════════════════════════════
     "the iso widgets could provide more detail on mouseover of the blocks ... blocks could light up on mouseover ...
     iso and other widgets could animate and be otherwise visibly interactive ... better deep dives" (the owner).
       hover   any drawn part with a detail (data-tip, or a title the element turns into one) shows it in a floating
               card; the part lights up - every face of the same block (data-b), its group dimmer (data-g), the rest of
               the face dims
       click   a part that names an entity (data-ref: guest:<vmid>, host:<id>, mesh:<host> ...) opens it in the entity
               drawer; one that does not opens the place the record's data lives on, with the part's name as the
               entity; a host that asks for it (dive-on-click) opens the DEEP DIVE on a click anywhere else
       motion  a face rises in on its first real reading (iso faces in paint order, bars grow, columns rise); a figure
               counts from its last reading to the new one and a bar slides to its new length; all of it off under
               prefers-reduced-motion and when the record says frame.motion false
       dive    VeraWidget.dive(el | {record, data}) - a sheet over the page: the record at its largest (XL, as tall as
               the sheet allows), every row of what it read as a sortable, searchable table, the raw answer, and the
               way to the place it lives on */
  const reduceMotion = () => { try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; } };
  const postTop = (m) => { try { const t = (window.top && window.top !== window) ? window.top : window; t.postMessage(m, '*'); } catch (_) { try { window.parent.postMessage(m, '*'); } catch (__) { /* no host to tell */ } } };
  // open what a block is: its entity, else the record's place (with the block's name as the entity); true when something opened
  function openBlock(host, rec, ref, name) {
    try { host.dispatchEvent(new CustomEvent('widget:block', { bubbles: true, composed: true, detail: { record: rec, ref: ref || '', name: name || '' } })); } catch (_) {}
    if (ref) {
      if (window.veraEntityDrawer && typeof window.veraEntityDrawer.open === 'function') { try { window.veraEntityDrawer.open(ref); return true; } catch (_) {} }
      postTop({ type: 'vera:entity:open', ref: String(ref) }); return true;
    }
    const src = rec && typeof rec.source === 'string' ? rec.source : '';
    const place = (rec && rec.open) || (typeof window.placeFor === 'function' ? window.placeFor(src, '') : '');
    if (!place) return false;
    if (typeof window.openPlace === 'function') { try { if (window.openPlace(place, name || '')) return true; } catch (_) {} }
    postTop({ type: 'vera:place:open', place: String(place), entity: String(name || '') }); return true;
  }
  const TIP_CSS = '.vw-tip{position:fixed;z-index:2147483000;pointer-events:none;max-width:320px;padding:7px 9px;border-radius:7px;background:var(--s1,var(--bg1,#15171c));color:var(--t1,var(--text,#d8dce4));box-shadow:0 8px 28px -8px rgba(0,0,0,.6),0 0 0 1px var(--bd2,rgba(255,255,255,.14));font-family:var(--f-ui,var(--sans,system-ui,sans-serif));font-size:11px;line-height:1.45;white-space:normal}'
    + '.vw-tip b{display:block;font-size:12px;font-weight:600;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-tip span{display:block;color:var(--t2,var(--dim2,#8a92a0));font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.vw-tip em{display:block;margin-top:4px;font-style:normal;font-size:10px;color:var(--acc,#6ea8d8)}'
    + '.vw-tip{max-width:360px}.vw-tip .vw-tkv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:1px 10px;margin:4px 0 2px}.vw-tip .vw-tkv > i{font-style:normal;color:var(--t2,var(--dim2,#8a92a0));font-size:10.5px;white-space:nowrap}'
    + '.vw-tip .vw-tkv > u{text-decoration:none;font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:10.5px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vw-tip .vw-tkv i.st{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}'
    + '.vw-tip small{display:block;margin-top:5px;padding-top:4px;border-top:1px solid var(--bd,rgba(255,255,255,.08));font-size:9.5px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.vw-tip a{color:inherit;text-decoration:none}'
    + ':host{position:relative}.vw-as{position:absolute;top:4px;right:4px;z-index:6;opacity:0;transition:opacity .15s}:host(:hover) .vw-as,.vw-as.open{opacity:1}:host-context(.dash-grid.editing) .vw-as{display:none}'
    + '.vw-as-b{all:unset;cursor:pointer;width:22px;height:20px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;font-size:12px;color:var(--b-t2,#8a92a0);background:var(--b-s2,#1f232b);box-shadow:inset 0 0 0 1px var(--b-bd,rgba(255,255,255,.12))}.vw-as-b:hover{color:var(--b-ac,#6ea8d8)}'
    + '.vw-as-m{position:fixed;z-index:2147483001;min-width:170px;display:flex;flex-direction:column;padding:4px;border-radius:8px;background:var(--b-s1,var(--s1,#15171c));box-shadow:0 10px 30px -10px rgba(0,0,0,.65),inset 0 0 0 1px var(--b-bd,rgba(255,255,255,.12))}'
    + '.vw-as-m > small{padding:3px 8px 4px;font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--b-t3,#6b7280)}.vw-as-m button{all:unset;cursor:pointer;display:flex;gap:8px;align-items:center;padding:4px 8px;border-radius:5px;font-size:11.5px;color:var(--b-t1,#d8dce4)}'
    + '.vw-as-m button:hover{background:var(--b-s2,#1f232b)}.vw-as-m button.on{color:var(--b-ac,#6ea8d8)}.vw-as-m button i{width:16px;height:14px;display:inline-flex;align-items:center;justify-content:center;font-style:normal;color:var(--b-t2,#8a92a0)}.vw-as-m button i svg{width:14px;height:14px}';
  const TIP_CSS_S = fontScale(TIP_CSS);
  // the card is kept on the root: ':scope > .vw-tip' matches nothing inside a shadow root, so looking it up made a new card on
  // every move and never hid one (found live, 2026-09-27: three moves, three cards, all shown after the pointer left)
  function tipOf(root) { let t = root._vwTip; if (!t || !t.isConnected) { t = document.createElement('div'); t.className = 'vw-tip'; t.hidden = true; root.appendChild(t); root._vwTip = t; } return t; }
  function showTip(root, text, x, y, act, html) {
    const t = tipOf(root); const L = String(text || '').split('\n');
    if (html) t.innerHTML = html + (act ? '<em>' + esc(act) + '</em>' : '');
    else { if (!L[0] && L.length < 2) { t.hidden = true; return; }
    t.innerHTML = '<b>' + esc(L[0]) + '</b>' + L.slice(1, 12).map((l) => '<span>' + esc(l) + '</span>').join('') + (act ? '<em>' + esc(act) + '</em>' : ''); }
    t.hidden = false; const vw = window.innerWidth || 1200, vh = window.innerHeight || 800, r = t.getBoundingClientRect();
    t.style.left = Math.max(4, Math.min(vw - r.width - 6, x + 14)) + 'px'; t.style.top = Math.max(4, (y + 16 + r.height > vh) ? y - r.height - 10 : y + 16) + 'px';
  }
  // what the pointer is over: a block (data-b), else anything with a detail; a title is taken into data-tip once (the
  // browser's own tooltip would stand on top of the card)

  /* THE HOVER CARD (owner, 2026-09-27: "even better coverage on detail when an item in a widget is hovered or clicked"). A
     part that carries its item says what it is at a glance: its name, its fields formatted as the drawer formats them
     (status dots, dates, bytes), the nested ones counted, how many more there are, the widget and source it belongs to,
     and how many other items in the same answer share a value with it. Built once per part, not on every move. */
  function tipCard(host, el) {
    const raw = el.getAttribute('data-item'); let it; if (raw) { try { it = JSON.parse(raw); } catch (_) {} }
    if (!it || typeof it !== 'object' || Array.isArray(it)) return '';
    const rec = recOf(host), lines = String(el.getAttribute('data-tip') || el.getAttribute('data-name') || '').split('\n');
    const head = lines[0] || itemName(it, rec.title || rec.form);
    const ks = Object.keys(it).filter((k) => !/^_/.test(k) && it[k] !== '' && it[k] != null);
    const plain = ks.filter((k) => isPlain(it[k])).slice(0, 10), nest = ks.filter((k) => !isPlain(it[k])).slice(0, 4);
    const val = (k, v) => (typeof v === 'string' && /^\s*[\[{]/.test(v)) ? esc(v.slice(0, 60) + (v.length > 60 ? '…' : '')) : fieldVal(k, typeof v === 'string' && v.length > 90 ? v.slice(0, 90) + '…' : v);
    let rel = 0; try { if (host._data !== undefined) rel = relatedTo(host._data, it).length; } catch (_) {}
    const more = ks.length - plain.length - nest.length;
    return '<b>' + esc(head) + '</b>'
      + (plain.length ? '<div class="vw-tkv">' + plain.map((k) => '<i>' + esc(k.replace(/_/g, ' ')) + '</i><u>' + val(k, it[k]) + '</u>').join('') + '</div>' : lines.slice(1, 8).map((l) => '<span>' + esc(l) + '</span>').join(''))
      + (nest.length ? '<span>' + nest.map((k) => esc(k.replace(/_/g, ' ')) + ' · ' + (Array.isArray(it[k]) ? it[k].length + (it[k].length === 1 ? ' item' : ' items') : Object.keys(it[k]).length + ' keys')).join('   ') + '</span>' : '')
      + (more > 0 ? '<span>+ ' + more + ' more field' + (more === 1 ? '' : 's') + '</span>' : '')
      + '<small>' + esc([rec.title || rec.form, typeof rec.source === 'string' ? rec.source : ''].filter(Boolean).join(' · ')) + (rel ? ' · ' + rel + ' related' : '') + '</small>';
  }

  /* VIEW AS (owner, 2026-09-27: "some widgets could transform to others to give a different view instead of haveing tonnes of
     realted spread out widgets"). A widget offers the forms of its data's family - only those that would draw this answer -
     and turns into the one chosen, in place; the choice is kept per widget, "as made" goes back. */
  const NO_VIEW = /^(panel|composite|calnav|month|schedule|calendar|agenda|vgraph|terminal|media|diff|code|markdown|error|string|split-flap|frame|announcement|globe|candles|context_graph|structgraph)$/;
  const VIEW_FAMILY = { series: ['trace', 'lines', 'area', 'step', 'scope', 'bars', 'table', 'json'], values: ['bars', 'column', 'ranked', 'lollipop', 'donut', 'treemap', 'pills', 'kv', 'table', 'json'],
    parts: ['donut', 'treemap', 'stacked-bar', 'waffle', 'bars', 'table', 'json'], items: ['table', 'rows', 'cards', 'list', 'stack', 'json'], events: ['log', 'lane', 'timeline', 'feed', 'table', 'json'],
    level: ['radial', 'gauge', 'meter', 'dial', 'counter', 'hero', 'json'], stages: ['stepper', 'pipeline', 'progress', 'funnel', 'gantt', 'table', 'json'], points: ['scatter', 'table', 'json'],
    matrix: ['heat', 'matrix', 'dots', 'table', 'json'], graph: ['graph', 'minigraph', 'pipes', 'json'], rate: ['turbine', 'ticker', 'json'] };
  function viewsFor(rec, form0, data) {
    const f0 = canon(form0), shape = DRAWN[f0]; if (!shape || NO_VIEW.test(f0) || data === undefined) return [];
    const out = [f0];
    (VIEW_FAMILY[shape] || ['table', 'json']).forEach((f) => { if (out.includes(f) || typeof R[f] !== 'function') return; try { if (!isEmpty(dataFor(mapped(rec, f, data), f))) out.push(f); } catch (_) {} });
    return out.slice(0, 9);
  }
  function partAt(root, target) {
    let el = target && target.closest ? target.closest('[data-b],[data-tip],[title],[data-item]') : null; if (!el || !root.contains(el) || el.classList.contains('vw-tip')) return null;
    if (el.hasAttribute('title')) { if (!el.hasAttribute('data-tip')) el.setAttribute('data-tip', el.getAttribute('title')); el.removeAttribute('title'); }
    return el;
  }
  function light(root, el) {
    const wrap = root.querySelector('.vw-root') || root; root.querySelectorAll('.hot,.warm').forEach((x) => x.classList.remove('hot', 'warm'));
    if (!el) { wrap.classList.remove('lit'); return; }
    const b = el.getAttribute('data-b'), g = el.getAttribute('data-g');
    (b ? root.querySelectorAll('[data-b="' + CSS_ESC(b) + '"]') : [el]).forEach((x) => x.classList.add('hot'));
    // the block's group glows too (the host a container runs on) - unless the group is most of the face, when it says nothing
    if (g) { const gs = root.querySelectorAll('[data-g="' + CSS_ESC(g) + '"]:not(.hot)'), all = root.querySelectorAll('[data-b]').length; if (gs.length < all * .6) gs.forEach((x) => x.classList.add('warm')); }
    wrap.classList.add('lit');
  }
  const CSS_ESC = (s) => String(s).replace(/["\\]/g, '\\$&');
  // the record as normalised, with the place it opens (normalise() keeps the schema's keys; `open` rides on the raw record)
  const recOf = (host) => host && host._rec ? Object.assign({}, host._rec, { open: (host._raw && host._raw.open) || host._rec.open || '' }) : {};
  function wireParts(host) {
    const root = host._sh; let cur = null;
    root.addEventListener('pointermove', (e) => {
      const el = partAt(root, e.target);
      if (el !== cur) { cur = el; light(root, el); host._tipHtml = el ? tipCard(host, el) : ''; }
      if (!el) { const t = root._vwTip; if (t) t.hidden = true; return; }
      const ref = el.getAttribute('data-ref'), rec = recOf(host);
      const act = host.hasAttribute('item-drawer') ? 'click · its data' + (ref ? ' (then open ' + ref + ')' : '') : (ref ? 'click · open ' + ref : ((el.hasAttribute('data-b') && (rec.open || rec.source)) ? 'click · open where it lives' : (host.hasAttribute('dive-on-click') ? 'click · the deep dive' : '')));
      showTip(root, el.getAttribute('data-tip') || el.getAttribute('data-name') || '', e.clientX, e.clientY, act, host._tipHtml);
    });
    root.addEventListener('pointerleave', () => { cur = null; light(root, null); const t = root._vwTip; if (t) t.hidden = true; });
    host.addEventListener('pointerleave', () => { cur = null; light(root, null); const t = root._vwTip; if (t) t.hidden = true; });
    root.addEventListener('click', (e) => {
      if (e.target.closest && e.target.closest('button,a,input,select,textarea,summary,[data-vb-set],[data-read],[data-act]')) return;
      if (host._vgHost && host._vgHost.contains(e.target)) return;   // the Vera graph handles its own clicks (a node opens the drawer through onNodeClick)
      // a day of a month: chosen (its group's schedules follow), then the drawer on it
      const selEl = e.target.closest && e.target.closest('[data-vb-sel]'); if (selEl && root.contains(selEl)) { const kv = selEl.getAttribute('data-vb-sel'), i = kv.indexOf(':'); host._ui[kv.slice(0, i)] = kv.slice(i + 1); host._calMove ? host._calMove(kv) : host.render(); }
      const hit = itemAt(host, e.target), rec = recOf(host);
      const ref = hit.ref || (hit.item && typeof hit.item === 'object' && !Array.isArray(hit.item) ? rowRef(hit.item) : '');
      const detail = { record: rec, item: hit.item, path: hit.path, ref, data: host._data, host };
      let go = true; try { go = host.dispatchEvent(new CustomEvent('widget:item', { bubbles: true, composed: true, cancelable: true, detail })); } catch (_) {}
      if (!go) { e.stopPropagation(); return; }
      if (host.hasAttribute('item-drawer') && !(host.closest && host.closest('.dash-grid.editing'))) { e.stopPropagation(); drawer(detail); return; }
      const el = hit.part; if (el && el.hasAttribute('data-b')) { if (openBlock(host, rec, el.getAttribute('data-ref') || '', el.getAttribute('data-name') || (el.getAttribute('data-tip') || '').split('\n')[0])) { e.stopPropagation(); return; } }
      if (host.hasAttribute('dive-on-click') && !(host.closest && host.closest('.dash-grid.editing'))) { e.stopPropagation(); dive(host); }
    });
  }
  // ── motion: what a render carries over from the one before it ──
  const TWEEN_SEL = '.vb-hero b,.vb-bigs b,.vb-dial > span,.vw-dtot,.vb-rw .v,.vw-therm b,.vw-kv b';
  const BAR_SEL = '.vb-rw .tr i,.vw-therm span i,.vb-bar2 i';
  const numParts = (s) => { const m = String(s == null ? '' : s).trim().match(/^([^0-9-]*)(-?[0-9][0-9,]*(?:\.[0-9]+)?)(.*)$/); return m ? { pre: m[1], n: parseFloat(m[2].replace(/,/g, '')), dec: (m[2].split('.')[1] || '').length, comma: m[2].indexOf(',') >= 0, post: m[3] } : null; };
  function motionBefore(root) { return { tw: [...root.querySelectorAll(TWEEN_SEL)].map((e) => e.textContent), bars: [...root.querySelectorAll(BAR_SEL)].map((e) => e.style.width) }; }
  function motionAfter(root, was) {
    if (!was || reduceMotion()) return;
    const now = [...root.querySelectorAll(TWEEN_SEL)];
    now.forEach((e, i) => { const a = numParts(was.tw[i]), b = numParts(e.textContent); if (!a || !b || a.pre !== b.pre || a.post !== b.post || a.n === b.n || !isFinite(a.n) || !isFinite(b.n)) return;
      const t0 = performance.now(), D = 650; const show = (v) => { const s = v.toFixed(b.dec); e.textContent = b.pre + (b.comma ? Number(s).toLocaleString(undefined, { minimumFractionDigits: b.dec, maximumFractionDigits: b.dec }) : s) + b.post; };
      const step = (t) => { const k = Math.min(1, (t - t0) / D), q = 1 - Math.pow(1 - k, 3); show(a.n + (b.n - a.n) * q); if (k < 1) requestAnimationFrame(step); };
      show(a.n); requestAnimationFrame(step); });
    const bars = [...root.querySelectorAll(BAR_SEL)];
    bars.forEach((e, i) => { const w0 = was.bars[i], w1 = e.style.width; if (!w0 || !w1 || w0 === w1) return; e.style.transition = 'none'; e.style.width = w0; void e.offsetWidth; e.style.transition = 'width .6s cubic-bezier(.2,.7,.2,1)'; e.style.width = w1; });
  }
  // ── the deep dive ──
  const DIVE_CSS = '.vw-dive-scrim{position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;animation:vw-dfade .18s ease-out}@keyframes vw-dfade{from{opacity:0}}'
    + '.vw-dive{width:min(1180px,calc(100vw - 32px));height:min(860px,calc(100vh - 32px));display:flex;flex-direction:column;background:var(--s1,var(--bg1,#15171c));color:var(--t1,var(--text,#d8dce4));border-radius:12px;box-shadow:0 30px 80px -20px rgba(0,0,0,.7),0 0 0 1px var(--bd2,rgba(255,255,255,.12));overflow:hidden;font-family:var(--f-ui,var(--sans,system-ui,sans-serif))}'
    + '.vw-dive-hd{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--bd,rgba(255,255,255,.08))}.vw-dive-hd h3{margin:0;font-size:15px;font-weight:600}.vw-dive-hd small{font-family:var(--f-mono,var(--mono,monospace));font-size:11px;color:var(--t3,var(--dim,#6b7280))}.vw-dive-hd .sp{flex:1}'
    + '.vw-dive-hd button{font:inherit;font-size:12px;color:inherit;background:var(--s2,var(--bg2,#1f232b));border:1px solid var(--bd2,rgba(255,255,255,.12));border-radius:6px;padding:5px 10px;cursor:pointer}.vw-dive-hd button:hover{border-color:var(--acc,#6ea8d8)}'
    + '.vw-dive-bd{flex:1;min-height:0;overflow:auto;padding:14px 16px;display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:14px;align-content:start}'
    + '.vw-dive-main{grid-column:1 / -1;height:min(46vh,420px);min-height:220px;background:var(--s0,var(--bg0,#101216));border-radius:10px;padding:12px;box-sizing:border-box}.vw-dive-main vera-widget{height:100%}'
    + '.vw-dive-sec{min-width:0;background:var(--s0,var(--bg0,#101216));border-radius:10px;padding:10px 12px}.vw-dive-sec h4{margin:0 0 8px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--t2,var(--dim2,#8a92a0))}'
    + '.vw-dive-sec pre{margin:0;max-height:340px;overflow:auto;font-family:var(--f-mono,var(--mono,monospace));font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:var(--t2,var(--dim2,#8a92a0))}.vw-dive-kids{grid-column:1 / -1;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}.vw-dive-kids > div{height:220px;background:var(--s0,var(--bg0,#101216));border-radius:10px;padding:8px}';
  // every row of an answer, as the form would read it: rows, else named values, else a dict of things, else its plain fields
  function diveRows(rec, data) {
    if (data == null) return [];
    const form = canon(rec && rec.form); let d = mapped(rec || {}, form, data); d = dataFor(d, form);
    let rw = rows(d); if (rw.length) return rw.slice(0, 500);
    const kv = keyed(d); if (kv.length) return kv.map((x) => ({ name: x[0], value: x[1] }));
    if (d && typeof d === 'object' && !Array.isArray(d)) { const ks = Object.keys(d);
      if (ks.length && ks.every((k) => d[k] && typeof d[k] === 'object' && !Array.isArray(d[k]))) return ks.map((k) => Object.assign({ name: k }, d[k]));
      return ks.filter((k) => d[k] == null || typeof d[k] !== 'object').map((k) => ({ name: k, value: d[k] })); }
    if (Array.isArray(d)) return d.map((v, i) => ({ name: String(i), value: v }));
    return [];
  }
  function dive(src) {
    if (typeof document === 'undefined') return null;
    const host = src && src._rec ? src : null; const rec = host ? recOf(host) : Object.assign(normalise((src && src.record) || src || {}), { open: ((src && src.record) || src || {}).open || '' });
    const data = host ? host._data : (src && src.data); const kids = host ? host._kids : null;
    if (!document.getElementById('vw-dive-css')) { const st = document.createElement('style'); st.id = 'vw-dive-css'; st.textContent = fontScale(DIVE_CSS + TIP_CSS); document.head.appendChild(st); }
    const old = document.querySelector('.vw-dive-scrim'); if (old) old.remove();
    const sc = document.createElement('div'); sc.className = 'vw-dive-scrim';
    const place = rec.open || (typeof window.placeFor === 'function' ? window.placeFor(rec.source || '', '') : '');
    sc.innerHTML = '<div class="vw-dive" role="dialog" aria-label="' + esc(rec.title || rec.form) + '"><div class="vw-dive-hd"><h3>' + esc(rec.title || rec.form || 'widget') + '</h3><small>' + esc(rec.form + (rec.source ? ' · ' + rec.source : '')) + '</small><span class="sp"></span>'
      + (place ? '<button data-dv="place">Open ' + esc(place) + ' ↗</button>' : '') + '<button data-dv="refresh">↻ Read again</button><button data-dv="close">✕</button></div><div class="vw-dive-bd"><div class="vw-dive-main"></div></div></div>';
    const bd = sc.querySelector('.vw-dive-bd');
    // the record at its largest: its own element, XL, reading its own source
    const big = document.createElement('vera-widget'); big.setAttribute('size', 'xl'); big.setAttribute('bare', ''); big.setAttribute('dive', '');
    const r2 = Object.assign({}, rec, { frame: Object.assign({}, rec.frame, { size: 'xl', max_body: null }) }); if (data !== undefined && !rec.source) r2.data = data;
    big.setAttribute('record', JSON.stringify(r2)); sc.querySelector('.vw-dive-main').appendChild(big);
    const fill = (d) => {
      bd.querySelectorAll('.vw-dive-sec,.vw-dive-kids').forEach((x) => x.remove());
      if (rec.form === 'composite' && Array.isArray(rec.children)) { const g = document.createElement('div'); g.className = 'vw-dive-kids';
        rec.children.forEach((c, i) => { const k = c && typeof c.record === 'object' ? c.record : null; if (!k) return; const slot = String(c.slot || String.fromCharCode(97 + i)); const cell = document.createElement('div'); const w = document.createElement('vera-widget'); w.setAttribute('size', 'l');
          const kd = kids && kids[slot] && kids[slot].__read ? kids[slot].data : undefined; w.setAttribute('record', JSON.stringify(Object.assign({}, k, kd !== undefined ? { data: kd, source: '' } : {}, { title: k.title || slot }))); cell.appendChild(w); g.appendChild(cell); });
        bd.appendChild(g); }
      const rw = diveRows(rec, d);
      const t = document.createElement('div'); t.className = 'vw-dive-sec'; t.innerHTML = '<h4>Every row · ' + rw.length + '</h4>' + (rw.length ? '' : '<i style="color:var(--dim)">nothing read yet</i>');
      if (rw.length) { const tw = document.createElement('vera-widget'); tw.setAttribute('size', 'xl'); tw.setAttribute('bare', ''); tw.style.height = '360px'; tw.setAttribute('record', JSON.stringify({ form: 'table', title: 'Every row', data: rw, draw: { search: true } })); t.appendChild(tw); }
      const j = document.createElement('div'); j.className = 'vw-dive-sec'; let raw = ''; try { raw = JSON.stringify(d, null, 2); } catch (_) { raw = String(d); }
      j.innerHTML = '<h4>What ' + esc(rec.source || 'the record') + ' answered</h4><pre>' + esc(String(raw == null ? '—' : raw).slice(0, 60000)) + '</pre>';
      bd.appendChild(t); bd.appendChild(j);
    };
    fill(data);
    const close = () => { sc.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    sc.addEventListener('click', (e) => { if (e.target === sc) close(); const b = e.target.closest && e.target.closest('[data-dv]'); if (!b) return;
      if (b.dataset.dv === 'close') close();
      else if (b.dataset.dv === 'place') { if (typeof window.openPlace === 'function' && window.openPlace(place)) { close(); return; } postTop({ type: 'vera:place:open', place }); close(); }
      else if (b.dataset.dv === 'refresh' && rec.source && readable(rec.source)) { b.disabled = true; call((host && host.base) || '', rec.source, (rec.read && rec.read.args) || {}).then((v) => { fill(v); try { big.refresh && big.refresh(); } catch (_) {} }, () => {}).then(() => { b.disabled = false; }); } });
    document.body.appendChild(sc);
    return sc;
  }

  /* ══ THE DATA DRAWER (the widget review, round 3) ════════════════════════════════════════════════════════════════
     VeraWidget.drawer({record, item, path, ref, data, host}) - a panel on the right of the page that holds the widget
     (in an embedded panel frame, that frame's page): the item whole - every field, nested values folded open - where
     it came from (the record's source, its arguments, the part of the answer), the items RELATED to it (other rows of
     the same answer that share an id, a name, a host, a node, a ref with it - click one to follow it; ‹ goes back), and
     its actions: open its entity (the estate drawer), open its place, the deep dive, copy its JSON.
     THE GESTURE, on every dashboard (VeraDash sets item-drawer on its tiles):
       click a part (an iso block, a table row, a bar, a slice, a pill, a day, an event, a composite's section)
                                           → the drawer, on that part
       click the tile's face anywhere else  → the drawer, on the whole answer
       in the drawer: Open <ref>            → the entity (the estate drawer; vera:entity:open to the harness)
                      Open <place>          → where the data lives; Deep dive → the dive sheet; Copy JSON
       ⤢ in the tile's head                 → the deep dive
     Every surface sets item-drawer: the dashboards, the canvas's items, the chat's and the docked menus' widgets. A host
     without it (the widget sheet's preview, the gallery) keeps the old gesture: a block with a ref opens its entity. A
     frame too narrow for the drawer hands it up to the page around it (the chat's menu → the harness).
     Every click still dispatches 'widget:item' (bubbling, composed, cancelable) first; a host that handles it itself
     calls preventDefault(). */
  const REL_KEYS = ['id', 'name', 'host', 'host_id', 'node', 'vmid', 'ref', 'ssh_host_id', 'session_id', 'branch', 'label', 'Names', 'Image', 'calendar', 'trigger', 'instance', 'model', 'source', 'owner', 'uid'];
  function relatedTo(answer, item, max) {
    max = max || 24; if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const want = {}; REL_KEYS.forEach((k) => { const v = item[k]; if ((typeof v === 'string' || typeof v === 'number') && String(v).length >= 2 && !/^(true|false|null|none|unknown|local|0)$/i.test(String(v))) want[k] = String(v); });
    const vals = new Set(Object.values(want)); if (!vals.size) return [];
    const self = (() => { try { return JSON.stringify(item); } catch (_) { return ''; } })(); const hits = [], share = {}; let rowsN = 0, seen = 0;
    const walk = (x, path, depth) => { if (depth > 5 || x == null || typeof x !== 'object' || ++seen > 20000) return;
      if (Array.isArray(x)) { x.forEach((v, i) => walk(v, path + '[' + i + ']', depth + 1)); return; }
      rowsN++; const via = REL_KEYS.filter((k) => (typeof x[k] === 'string' || typeof x[k] === 'number') && vals.has(String(x[k])));
      via.forEach((k) => { const key = k + '=' + x[k]; share[key] = (share[key] || 0) + 1; });
      if (via.length && path) { let j = ''; try { j = JSON.stringify(x); } catch (_) {} if (j !== self && j.length < 200000) hits.push({ path, row: x, via }); }
      Object.keys(x).forEach((k) => { const v = x[k]; if (v && typeof v === 'object') walk(v, path ? path + '.' + k : k, depth + 1); }); };
    walk(answer, '', 0);
    // a value most of the answer shares (every guest on node corp, every event in one calendar) says nothing: it is not a link
    const common = (k, v) => { const n = share[k + '=' + v] || 0; return n > 3 && (n > rowsN * .3 || n > hits.length * .5); };
    return hits.map((h) => ({ path: h.path, row: h.row, via: h.via.filter((k) => !common(k, h.row[k])).map((k) => k + ' ' + h.row[k]) })).filter((h) => h.via.length)
      .sort((a, b2) => b2.via.length - a.via.length).slice(0, max); }
  const DRAWER_CSS = '.vw-drawer{position:fixed;top:0;right:0;bottom:0;z-index:9400;width:min(460px,94vw);display:flex;flex-direction:column;background:var(--s1,var(--bg1,#15171c));color:var(--t1,var(--text,#d8dce4));box-shadow:-18px 0 50px -20px rgba(0,0,0,.65),-1px 0 0 var(--bd2,rgba(255,255,255,.12));font-family:var(--f-ui,var(--sans,system-ui,sans-serif));font-size:12px;animation:vw-drin .2s cubic-bezier(.2,.7,.2,1)}@keyframes vw-drin{from{transform:translateX(24px);opacity:0}}'
    + '.vw-drawer header{display:flex;align-items:flex-start;gap:8px;padding:12px 14px 10px;border-bottom:1px solid var(--bd,rgba(255,255,255,.08))}.vw-drawer header .tt{flex:1;min-width:0}.vw-drawer header b{display:block;font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vw-drawer header small{display:block;font-family:var(--f-mono,var(--mono,monospace));font-size:10.5px;color:var(--t3,var(--dim,#6b7280));overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    + '.vw-drawer button{font:inherit;font-size:11.5px;color:inherit;background:var(--s2,var(--bg2,#1f232b));border:1px solid var(--bd2,rgba(255,255,255,.12));border-radius:6px;padding:4px 9px;cursor:pointer}.vw-drawer button:hover{border-color:var(--acc,#6ea8d8)}.vw-drawer button.x,.vw-drawer button.bk{padding:2px 8px}.vw-drawer button.pri{border-color:var(--acc,#6ea8d8);color:var(--acc,#6ea8d8)}'
    + '.vw-dr-acts{display:flex;flex-wrap:wrap;gap:6px;padding:8px 14px;border-bottom:1px solid var(--bd,rgba(255,255,255,.08))}.vw-dr-bd{flex:1;min-height:0;overflow:auto;padding:4px 14px 18px}'
    + '.vw-dr-bd h4{margin:14px 0 6px;font-size:10.5px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--t2,var(--dim2,#8a92a0))}'
    + '.vw-dr-kv{display:grid;grid-template-columns:minmax(90px,34%) minmax(0,1fr);gap:1px 10px}.vw-dr-kv > span{padding:3px 0;border-bottom:1px solid var(--bd,rgba(255,255,255,.06));min-width:0;overflow-wrap:anywhere}.vw-dr-kv > span.k{color:var(--t2,var(--dim2,#8a92a0));font-size:11px}.vw-dr-kv > span.v{font-family:var(--f-mono,var(--mono,monospace));font-size:11.5px}.vw-dr-kv i.st{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}'
    + '.vw-dr-rel{display:flex;flex-direction:column;gap:3px}.vw-dr-rel > div{display:flex;gap:8px;align-items:baseline;padding:5px 8px;border-radius:6px;background:var(--s2,var(--bg2,#1f232b));cursor:pointer}.vw-dr-rel > div:hover{box-shadow:inset 0 0 0 1px var(--acc,#6ea8d8)}.vw-dr-rel b{font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.vw-dr-rel small{font-family:var(--f-mono,var(--mono,monospace));font-size:10px;color:var(--t3,var(--dim,#6b7280));white-space:nowrap}'
    + '.vw-dr-src{font-family:var(--f-mono,var(--mono,monospace));font-size:11px;color:var(--t2,var(--dim2,#8a92a0));white-space:pre-wrap;overflow-wrap:anywhere}'
    + '.vw-dr-json{font-family:var(--f-mono,var(--mono,monospace));font-size:11.5px;line-height:1.6;padding:8px 10px 8px 22px;border-radius:7px;background:var(--s2,var(--bg2,#1f232b));box-shadow:inset 0 0 0 1px var(--bd,rgba(255,255,255,.06));overflow-x:auto}'
    + '.vw-dr-json .l{white-space:pre-wrap;overflow-wrap:anywhere}.vw-dr-json details > .in{padding-left:16px;margin-left:2px;border-left:1px dashed var(--bd,rgba(255,255,255,.1))}'
    + '.vw-dr-json summary{cursor:pointer;list-style:none;border-radius:3px}.vw-dr-json summary::-webkit-details-marker{display:none}.vw-dr-json summary:hover{background:var(--s3,rgba(255,255,255,.04))}'
    + '.vw-dr-json summary::before{content:"\\25BE";display:inline-block;width:14px;margin-left:-14px;color:var(--t3,var(--dim,#6b7280))}.vw-dr-json details:not([open]) > summary::before{content:"\\25B8"}.vw-dr-json details:not([open]) > summary::after{content:" \\2026";color:var(--t3,var(--dim,#6b7280))}'
    + '.vw-dr-json i{font-style:normal}.vw-dr-json .k{color:var(--t1,var(--text,#d8dce4))}.vw-dr-json .s{color:var(--ok,#5fc49a)}.vw-dr-json .m{color:var(--acc,#6ea8d8)}.vw-dr-json .b{color:var(--warn,#f5b341)}.vw-dr-json .n,.vw-dr-json .p{color:var(--t3,var(--dim,#6b7280))}'
    + '.vw-dr-json .c{color:var(--t3,var(--dim,#6b7280));font-size:10px;margin-left:8px;font-family:var(--f-ui,var(--sans,system-ui,sans-serif))}.vw-dr-json .t{font-size:9px;letter-spacing:.06em;text-transform:uppercase;margin-right:6px;padding:0 5px;border-radius:3px;color:var(--acc,#6ea8d8);box-shadow:inset 0 0 0 1px var(--acc,#6ea8d8)}'
    + '.vw-dr-kv .vw-dr-json{padding:4px 6px 4px 18px;font-size:11px}.vw-dr-argl{margin:8px 0 4px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--t3,var(--dim,#6b7280))}.vw-drawer button.on{border-color:var(--acc,#6ea8d8);color:var(--acc,#6ea8d8)}';

  /* THE DRAWER'S JSON (owner, 2026-09-27: "prettyfy all the json properly in the right hand details panel"). The tile's json
     form, drawn into the page, lost its styles (they live in the widget's shadow root) and cut every value to a line. This
     is JSON's own shape - keys quoted, braces and commas - coloured by kind, strings wrapped whole, a string that holds
     JSON shown as the JSON it holds, the first two levels open and the rest a click away. */
  function drJson(root) {
    let left = 4000;
    const inJson = (s) => { if (typeof s !== 'string' || s.length < 2 || !/^\s*[\[{]/.test(s)) return undefined; try { const o = JSON.parse(s); return o && typeof o === 'object' ? o : undefined; } catch (_) { return undefined; } };
    const leaf = (v) => v === null ? '<i class="n">null</i>' : typeof v === 'number' ? '<i class="m">' + esc(String(v)) + '</i>' : typeof v === 'boolean' ? '<i class="b">' + v + '</i>' : '<i class="s">' + esc(JSON.stringify(String(v).length > 20000 ? String(v).slice(0, 20000) + '…' : String(v))) + '</i>';
    const node = (k, v, depth, last) => {
      if (--left < 0) return depth ? '' : '<div class="l"><i class="n">…</i></div>';
      let key = typeof k === 'string' ? '<i class="k">' + esc(JSON.stringify(k)) + '</i><i class="p">: </i>' : '';
      const comma = last ? '' : '<i class="p">,</i>';
      const held = inJson(v); if (held !== undefined) { v = held; key += '<i class="t" title="a string holding JSON, shown as that JSON">json</i>'; }
      if (v === null || typeof v !== 'object') return '<div class="l">' + key + leaf(v) + comma + '</div>';
      const arr = Array.isArray(v), ks = arr ? v.map((_, i) => i) : Object.keys(v), n = ks.length, o = arr ? '[' : '{', c = arr ? ']' : '}';
      if (!n) return '<div class="l">' + key + '<i class="p">' + o + c + '</i>' + comma + '</div>';
      const shown = Math.min(n, 500);
      const kids = ks.slice(0, shown).map((kk, i) => node(arr ? i : kk, v[kk], depth + 1, i === shown - 1 && n <= 500)).join('') + (n > 500 ? '<div class="l"><i class="n">… ' + (n - 500) + ' more</i></div>' : '');
      return '<details' + (depth < 2 ? ' open' : '') + '><summary>' + key + '<i class="p">' + o + '</i><i class="c">' + n + (arr ? (n === 1 ? ' item' : ' items') : (n === 1 ? ' key' : ' keys')) + '</i></summary><div class="in">' + kids + '</div><div class="l"><i class="p">' + c + '</i>' + comma + '</div></details>';
    };
    return '<div class="vw-dr-json">' + node(null, root, 0, true) + '</div>';
  }
  const isPlain = (v) => v == null || typeof v !== 'object';
  /* an item with no name field is named by what identifies it (a request log row: its model, else its id) - the drawer's
     head and the hover card said the widget's title for every row */
  const ID_KEYS = ['model', 'host', 'hostname', 'node', 'path', 'file', 'key', 'slug', 'url', 'email', 'cap', 'capability', 'req_id', 'request_id', 'job_id', 'run_id', 'uuid', 'id'];
  const idName = (it) => { for (const k of ID_KEYS) { const v = it[k]; if ((typeof v === 'string' && v.trim()) || typeof v === 'number') return String(v).slice(0, 80); }
    const k2 = Object.keys(it).find((k) => /_(id|name|key)$/.test(k) && (typeof it[k] === 'string' || typeof it[k] === 'number') && String(it[k]).trim()); return k2 ? String(it[k2]).slice(0, 80) : ''; };
  const fieldVal = (k, v) => { if (v === null || v === undefined) return '<i style="opacity:.5">—</i>'; if (typeof v === 'boolean') return '<i class="st" style="background:' + (v ? 'var(--ok,#28c28a)' : 'var(--warn,#f5b341)') + '"></i>' + (v ? 'yes' : 'no');
    if (typeof v === 'number') return esc(TIP_RAW.test(k) ? String(v) : ((/(^|_)(created|updated|started|ended|at|ts|time|last_run|next_run)$/i.test(k) && v > 1e9 && v < 4e10) ? new Date(v * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' · ' + v : (/bytes?$|_b$/.test(k) ? fmtBytesS(v) + ' · ' + v : fmt(v))));
    const s = String(v); if (/^(status|state|health|level|severity)$/i.test(k)) return '<i class="st" style="background:' + stCol(s).replace(/var\(--b-(ac\d?|t3)\)/, (m, x) => ({ ac: 'var(--acc,#6ea8d8)', ac2: 'var(--ok,#28c28a)', ac3: 'var(--warn,#f5b341)', ac4: 'var(--err,#ef5b5b)', t3: 'var(--dim,#6b7280)' }[x] || m)) + '"></i>' + esc(s);
    if (/^\s*[\[{]/.test(s)) { try { const o = JSON.parse(s); if (o && typeof o === 'object') return drJson(o); } catch (_) {} }
    if (/^https?:\/\//.test(s)) return '<a href="' + esc(s) + '" target="_blank" rel="noopener" style="color:var(--acc,#6ea8d8)">' + esc(s) + '</a>'; return esc(s.length > 2000 ? s.slice(0, 2000) + '…' : s); };
  const itemName = (it, fb) => (it && typeof it === 'object' && !Array.isArray(it)) ? String(nameOf(it) || it.date || it.title || idName(it) || fb || 'item') : (Array.isArray(it) ? (fb || 'the answer') + ' · ' + it.length + ' items' : String(it ?? fb ?? 'item'));
  let _drawer = null;
  function drawer(detail) {
    if (typeof document === 'undefined' || !detail) return null;
    const doc = (detail.host && detail.host.ownerDocument) || document;
    /* a frame too narrow for the drawer (the chat's menu, a docked side) hands it to the page around it when that page
       draws widgets too (the harness does) - the item, its record and its answer as plain data; else it draws here */
    try { const win = doc.defaultView, up = win && win.parent;
      if (up && up !== win && (win.innerWidth || 0) < 560 && up.VeraWidget && typeof up.VeraWidget.drawer === 'function') {
        const plain = (v) => { try { const j = JSON.stringify(v); return j === undefined || j.length > 400000 ? undefined : JSON.parse(j); } catch (_) { return undefined; } };
        return up.VeraWidget.drawer({ record: plain(detail.record) || {}, item: plain(detail.item), path: detail.path || '', ref: detail.ref || '', data: plain(detail.data), host: null }); } } catch (_) {}
    if (!doc.getElementById('vw-drawer-css')) { const st = doc.createElement('style'); st.id = 'vw-drawer-css'; st.textContent = fontScale(DRAWER_CSS); doc.head.appendChild(st); }
    ensureCss(doc);
    if (!_drawer || !_drawer.el.isConnected) {
      const el = doc.createElement('aside'); el.className = 'vw-drawer'; el.setAttribute('role', 'complementary'); el.setAttribute('aria-label', 'the item\'s data');
      let view = ''; try { view = localStorage.getItem('vera.drawer.view') || ''; } catch (_) {}
      _drawer = { el, stack: [], view }; doc.body.appendChild(el);
      doc.addEventListener('keydown', (e) => { if (e.key === 'Escape' && _drawer && _drawer.el.isConnected && !doc.querySelector('.vw-dive-scrim')) { _drawer.el.remove(); } }, true);
    }
    _drawer.stack.push(detail); if (_drawer.stack.length > 30) _drawer.stack.shift();
    paintDrawer(); return _drawer.el;
  }
  function paintDrawer() {
    const D = _drawer, el = D.el, cur = D.stack[D.stack.length - 1]; if (!cur) { el.remove(); return; }
    const rec = cur.record || {}, it = cur.item, host = cur.host || null; const name = itemName(it, rec.title || rec.form);
    const ref = cur.ref || (it && typeof it === 'object' && !Array.isArray(it) ? rowRef(it) : ''); const src = typeof rec.source === 'string' ? rec.source : '';
    const place = rec.open || (typeof window.placeFor === 'function' ? window.placeFor(src, '') : '');
    const obj = (it && typeof it === 'object') ? it : { value: it };
    const plain = Array.isArray(obj) ? [] : Object.keys(obj).filter((k) => isPlain(obj[k])), nested = Array.isArray(obj) ? ['(items)'] : Object.keys(obj).filter((k) => !isPlain(obj[k]));
    const rel = cur.data !== undefined && !Array.isArray(it) ? relatedTo(cur.data, it) : [];
    const args = rec.read && rec.read.args && Object.keys(rec.read.args).length ? JSON.stringify(resolveArgs(rec.read.args, host && host._ui), null, 1) : '';
    el.innerHTML = '<header>' + (D.stack.length > 1 ? '<button class="bk" data-dr="back" title="back">‹</button>' : '') + '<div class="tt"><b title="' + esc(name) + '">' + esc(name) + '</b><small>' + esc([rec.title, cur.path, rec.form, src].filter(Boolean).join(' · ')) + '</small></div><button class="x" data-dr="close" title="close (Esc)">✕</button></header>'
      + '<div class="vw-dr-acts">' + (ref ? '<button class="pri" data-dr="entity">Open ' + esc(ref) + ' ↗</button>' : '') + (place ? '<button data-dr="place">Open ' + esc(place) + '</button>' : '') + '<button data-dr="dive">Deep dive ⤢</button><button data-dr="view" class="' + (D.view === 'json' ? 'on' : '') + '" title="Show the item as fields or as JSON">{ } JSON</button><button data-dr="copy">Copy JSON</button></div>'
      + '<div class="vw-dr-bd">'
      + drMedia(obj, host)
      + (D.view === 'json' ? '<h4>The item · JSON</h4>' + drJson(it) : '')
      + (D.view !== 'json' && plain.length ? '<h4>Fields · ' + (plain.length + nested.length) + '</h4><div class="vw-dr-kv">' + plain.map((k) => '<span class="k">' + esc(k.replace(/_/g, ' ')) + '</span><span class="v">' + fieldVal(k, obj[k]) + '</span>').join('') + '</div>' : '')
      + (D.view !== 'json' && nested.length ? '<h4>' + (plain.length ? 'Nested' : 'Everything') + '</h4>' + drJson(Array.isArray(obj) ? obj : nested.reduce((o2, k) => { o2[k] = obj[k]; return o2; }, {})) : '')
      + (rel.length ? '<h4>Related · ' + rel.length + ' in the same answer</h4><div class="vw-dr-rel">' + rel.map((r, i) => '<div data-dr-rel="' + i + '"><b>' + esc(itemName(r.row, r.path)) + '</b><small>' + esc(r.via.join(' · ')) + '</small></div>').join('') + '</div>' : '')
      + '<h4>Where it comes from</h4><div class="vw-dr-src">' + esc((src ? src : 'the record\'s own data') + (cur.path ? '\npart ' + cur.path : '') + (rec.id ? '\nrecord ' + rec.id : '')) + '</div>' + (args ? '<div class="vw-dr-argl">args</div>' + drJson(JSON.parse(args)) : '')
      + '</div>';
    el.onclick = (e) => { const b = e.target.closest && e.target.closest('[data-dr],[data-dr-rel]'); if (!b) return;
      if (b.hasAttribute('data-dr-rel')) { const r = rel[+b.getAttribute('data-dr-rel')]; if (r) { D.stack.push({ record: rec, item: r.row, path: r.path, ref: rowRef(r.row), data: cur.data, host }); paintDrawer(); } return; }
      const a = b.getAttribute('data-dr');
      if (a === 'close') { el.remove(); D.stack = []; }
      else if (a === 'back') { D.stack.pop(); paintDrawer(); }
      else if (a === 'view') { D.view = D.view === 'json' ? '' : 'json'; try { localStorage.setItem('vera.drawer.view', D.view); } catch (_) {} paintDrawer(); }
      else if (a === 'entity') openBlock(host || el, rec, ref, name);
      else if (a === 'place') openBlock(host || el, rec, '', name);
      else if (a === 'dive') dive(host && host._rec ? host : { record: rec, data: cur.data });
      else if (a === 'copy') { let j = ''; try { j = JSON.stringify(it, null, 2); } catch (_) { j = String(it); } try { navigator.clipboard.writeText(j); b.textContent = 'Copied'; } catch (_) { b.textContent = 'Copy failed'; } } };
  }
  // what a click is on: a part carrying its item, a composite's section, a block, or the tile itself
  function itemAt(host, target) {
    const root = host._sh; const part = target && target.closest ? target.closest('[data-item],[data-slot],[data-b]') : null;
    if (part && root.contains(part)) {
      const raw = part.getAttribute('data-item');
      if (raw) { try { return { item: JSON.parse(raw), path: part.getAttribute('data-path') || '', ref: part.getAttribute('data-ref') || '', part }; } catch (_) {} }
      if (part.hasAttribute('data-slot')) { const slot = part.getAttribute('data-slot'), k = host._kids && host._kids[slot]; const ttl = part.querySelector('.vw-slot-h,.k'); return { item: k && k.__read ? k.data : (k && !k.__pending ? k : undefined), path: 'section ' + (ttl ? ttl.textContent.trim().split('\n')[0] : slot), ref: '', part }; }
      if (part.hasAttribute('data-b')) return { item: { name: part.getAttribute('data-name') || (part.getAttribute('data-tip') || '').split('\n')[0], detail: part.getAttribute('data-tip') || '' }, path: 'block', ref: part.getAttribute('data-ref') || '', part };
    }
    return { item: host._data, path: '', ref: '', part: null };
  }

  class VeraWidgetEl extends HTMLElement {
    constructor() { super(); this._sh = this.attachShadow({ mode: 'open' }); this._rec = null; this._data = undefined; this._empty = false; this._drawn = ''; this._timer = null; this._ro = null; this._auto = 'm'; this._kids = {}; this._ui = {}; wireParts(this); }
    static get observedAttributes() { return ['record', 'size', 'base', 'template-id', 'bare']; }
    get record() { return this._rec; }
    set record(v) {
      let j = ''; try { j = JSON.stringify(v); } catch (_) { j = ''; }
      if (j && j === this._recJson && this._rec) return;   // the same record again (a layout re-applied): keep the reading, the children and the timer
      this._recJson = j; this._raw = (v && typeof v === 'object') ? v : null; this._rec = normalise(v); this._data = (v && v.data !== undefined) ? v.data : undefined; this._empty = false; this._drawn = ''; this._viewAs = undefined; this._kids = {}; this._read = false; this._err = ''; if (this.isConnected) this._boot(); }
    get base() { return this.getAttribute('base') || window._veraBase || ''; }
    get size() { const s = this.getAttribute('size'); return s && s !== 'auto' && SIZES.includes(s) ? s : (s === 'auto' ? this._auto : (this._rec ? this._rec.frame.size : 'm')); }
    connectedCallback() {
      INSTANCES.add(this); ensureIso(this.base);
      // the calendar controls of this widget's group move it (vera:calnav on the document: {group, off, view})
      if (!this._calL) { this._calL = (e) => { const r = this._rec, dt = e.detail || {}; if (!r || e.target === this || !/^(month|schedule|calnav|agenda)$/.test(canon(r.form))) return; if (String((r.draw && r.draw.group) || 'cal') !== String(dt.group || 'cal')) return;
        if (dt.off != null) this._ui.off = +dt.off; if (dt.view) this._ui.view = dt.view; if (dt.sel !== undefined) this._ui.sel = dt.sel; if (hasArgTokens(r) && r.source) this.read(true); else this.render(); }; document.addEventListener('vera:calnav', this._calL); }
      const a = this.getAttribute('record'); if (a && !this._rec) { try { this.record = JSON.parse(a); } catch (_) { this._rec = normalise({}); } }
      if (window.ResizeObserver && !this._rz) { let last = 0; this._rz = new ResizeObserver(() => { const h = this.clientHeight || 0; if (Math.abs(h - last) > 12) { last = h; this._measured = ''; if (this._rec) this.render(); } }); this._rz.observe(this); }
      if (this.getAttribute('size') === 'auto' && window.ResizeObserver && !this._ro) { this._ro = new ResizeObserver(() => { const s = sizeForWidth(this.clientWidth || 300); if (s !== this._auto) { this._auto = s; this.render(); this.dispatchEvent(new CustomEvent('widget:resize', { bubbles: true, composed: true, detail: { size: s } })); } }); this._ro.observe(this); }
      this._boot();
    }
    disconnectedCallback() { INSTANCES.delete(this); if (this._calL) { document.removeEventListener('vera:calnav', this._calL); this._calL = null; } if (this._timer) { clearInterval(this._timer); this._timer = null; } if (this._retry) { clearTimeout(this._retry); this._retry = null; } if (this._ro) { this._ro.disconnect(); this._ro = null; } if (this._rz) { this._rz.disconnect(); this._rz = null; } }
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
      // a refresh nobody can see is skipped: a hidden lens, a background tab, a closed card (the Loop Lab page kept
      // every lens's tiles reading while one was shown, 2026-09-28); the next tick after it is shown reads
      const seen = () => !(typeof document !== 'undefined' && document.hidden) && this.isConnected && (this.offsetParent !== null || this.getClientRects().length > 0);
      if (comp && this._rec.read.refresh) this._timer = setInterval(() => { if (seen()) this._readKids(); }, every * 1000);
      else if (cap && this._rec.read.refresh && readable(cap)) this._timer = setInterval(() => { if (seen()) this.read(); }, every * 1000);
    }
    // a composite's children read on their own: each child with a readable source is called with its own args; a child
    // whose source is $subject.<path> takes that slice of the composite's one read (the composite's source, its args);
    // a child that carries data keeps it. What came back sits per slot and the composite draws it.
    async _readKids() {
      const rec = this._rec; if (!rec || rec.form !== 'composite' || !Array.isArray(rec.children)) return;
      const kids = rec.children.map((c, i) => ({ slot: String((c && c.slot) || String.fromCharCode(97 + i)), r: (c && typeof c.record === 'object') ? normalise(c.record) : null, own: c && typeof c.record === 'object' && c.record.data !== undefined }));
      let subj; const wants = kids.some((k) => k.r && /^\$subject/.test(k.r.source));
      // before anything has answered, every child that will be read says "reading…" (its slot never shows sample values)
      if (!this._kids || !Object.keys(this._kids).length) { const pend = {}; kids.forEach((k) => { if (k.r && !k.own && k.r.source && (/^\$subject/.test(k.r.source) ? (rec.source && readable(rec.source)) : readable(k.r.source))) pend[k.slot] = { __pending: true }; }); if (Object.keys(pend).length) { this._kids = pend; this.render(); } }
      if (wants && rec.source && readable(rec.source)) { try { subj = await this._call(rec.source, resolveArgs(rec.read.args, this._ui)); if (subj && typeof subj === 'object' && subj.error && Object.keys(subj).length <= 2) { this._err = String(subj.error).slice(0, 120); subj = undefined; } else this._err = ''; } catch (e) { subj = undefined; this._err = String(e && e.message || e).slice(0, 120); } }
      const out = Object.assign({}, this._kids || {});
      const keep = (slot, v, err) => { const prev = out[slot] && out[slot].__read ? out[slot] : null; out[slot] = { __read: true, data: err ? (prev ? prev.data : undefined) : v, err: err || '' }; };
      // what the subject answers is drawn now; every child that reads on its own is drawn as it lands
      const show = () => { if (this._rec !== rec) return; this._kids = Object.assign({}, out); if (subj !== undefined) this._data = subj; this.render(); };
      kids.forEach((k) => { if (!k.r || k.own || !/^\$subject/.test(k.r.source)) return; if (subj !== undefined) keep(k.slot, pick(subj, k.r.source.replace(/^\$subject\.?/, ''))); else if (this._err) keep(k.slot, undefined, this._err); });
      show();
      await Promise.all(kids.map(async (k) => { if (!k.r || k.own || /^\$subject/.test(k.r.source)) return;
        if (k.r.source && readable(k.r.source)) { try { const v = await this._call(k.r.source, resolveArgs(k.r.read.args, this._ui)); if (v && typeof v === 'object' && v.error && Object.keys(v).length <= 2) keep(k.slot, undefined, String(v.error).slice(0, 120)); else keep(k.slot, v); } catch (e) { keep(k.slot, undefined, String(e && e.message || e).slice(0, 120)); } show(); } }));
      if (this._rec !== rec) return; this._kids = out; if (subj !== undefined) this._data = subj; this.render();
      // a child (or the subject) that failed before it ever answered is asked again soon, not at the next full refresh
      if (Object.keys(out).some((s) => out[s] && out[s].err && out[s].data === undefined) || (wants && subj === undefined && this._err)) this._retrySoon(() => this._readKids());
      this.dispatchEvent(new CustomEvent('widget:refresh', { bubbles: true, composed: true, detail: { record: rec, data: this._data, kids: out } }));
    }
    async read(forced) {
      const cap = this._rec && this._rec.source; if (!cap) return;
      if (!forced && !readable(cap)) return;
      let res; try { res = await this._call(cap, resolveArgs(this._rec.read.args, this._ui)); } catch (e) { res = { error: String(e && e.message || e) }; }
      // a read that failed keeps the last reading, else the sample face (marked) — a tile never empties on a refresh
      if (res && typeof res === 'object' && res.error && Object.keys(res).length <= 2) { this._err = String(res.error).slice(0, 120); if (this._data === undefined) this._retrySoon(() => this.read()); this._read = this._data !== undefined; this.render(); return; }
      const got = !isEmpty(mapped(this._rec, this._rec.form, res));
      // a read that answered with nothing while nothing is shown yet: the sample face stays and the caption says so (a sandbox
      // without the store behind a source, a fresh instance) — the next refresh may bring the thing itself
      if (!got && this._data === undefined) { this._err = ''; this._empty = true; this._read = false; this.render(); return; }
      this._empty = false; this._err = ''; this._read = true; this._data = res; this._drawn = got ? formFor(this._rec, res) : ''; this.render();
      this.dispatchEvent(new CustomEvent('widget:refresh', { bubbles: true, composed: true, detail: { record: this._rec, data: res } }));
    }
    refresh() { return this.read(true); }
    // a month or a view chosen here: this widget re-reads when its arguments follow the month, and every calendar of its
    // group on the page follows (vera:calnav)
    _calMove(kv) { const r = this._rec; if (!r) return; if (/^off:/.test(kv)) this._ui.sel = '';
      if (hasArgTokens(r) && r.source) this.read(true); else this.render();
      try { document.dispatchEvent(new CustomEvent('vera:calnav', { detail: { group: String((r.draw && r.draw.group) || 'cal'), off: +(this._ui.off || 0), view: this._ui.view || undefined, sel: this._ui.sel } })); } catch (_) {} }
    // one retry at a time, soon (RETRY_S), for a reading that failed before it ever answered - its next full refresh may be
    // minutes away (a backup census reads every five), and until then the tile would have nothing true to show
    _retrySoon(fn) { if (this._retry || !this.isConnected) return; this._retry = setTimeout(() => { this._retry = null; if (this.isConnected) fn(); }, RETRY_S * 1000); }
    _act(id) { this.dispatchEvent(new CustomEvent('widget:' + id, { bubbles: true, composed: true, detail: { record: this._rec, data: this._data, key: key(this._rec) } })); }
    // the ⇄ control: the forms of the data's family that would draw this answer; the pick redraws in place
    _viewAsWire(form0, form, rec) {
      if (this.hasAttribute('no-view-as') || (/^(xs|s)$/.test(this.size) && !this.hasAttribute('bare'))) return;
      const alts = viewsFor(rec, form0, this._data); if (alts.length < 2) return;
      const box = document.createElement('div'); box.className = 'vw-as';
      box.innerHTML = '<button type="button" class="vw-as-b" title="View this as another form">\u21c4</button><div class="vw-as-m" hidden><small>view as</small>'
        + alts.map((f) => '<button type="button" data-as="' + esc(f) + '" class="' + (f === form ? 'on' : '') + '"><i>' + glyphOf(f, this._data) + '</i>' + esc(f) + (f === canon(form0) ? ' \u00b7 as made' : '') + '</button>').join('') + '</div>';
      const m = box.querySelector('.vw-as-m'), b = box.querySelector('.vw-as-b');
      const close = () => { m.hidden = true; box.classList.remove('open'); document.removeEventListener('click', close, true); };
      b.addEventListener('click', (ev) => { ev.stopPropagation(); if (!m.hidden) { close(); return; } const r = b.getBoundingClientRect(); m.hidden = false; box.classList.add('open');
        const mw = m.offsetWidth || 170, mh = m.offsetHeight || 200; m.style.left = Math.max(4, Math.min((window.innerWidth || 1200) - mw - 4, r.right - mw)) + 'px'; m.style.top = ((r.bottom + 4 + mh > (window.innerHeight || 800)) ? Math.max(4, r.top - mh - 4) : r.bottom + 4) + 'px';
        setTimeout(() => document.addEventListener('click', close, true), 0); });
      m.addEventListener('click', (ev) => { const x = ev.target.closest && ev.target.closest('[data-as]'); if (!x) return; ev.stopPropagation(); const f = x.getAttribute('data-as');
        this._viewAs = f === canon(form0) ? '' : f; try { if (this._viewAs) localStorage.setItem('vera.widget.as.' + key(rec), this._viewAs); else localStorage.removeItem('vera.widget.as.' + key(rec)); } catch (_) {}
        close(); this.render(); this.dispatchEvent(new CustomEvent('widget:view-as', { bubbles: true, composed: true, detail: { record: rec, form: this._viewAs || form0 } })); });
      this._sh.appendChild(box);
    }
    render() {
      const rec = this._rec || normalise({}); const size = this.size; const form0 = this._drawn || rec.form;
      if (this._viewAs === undefined) { try { this._viewAs = localStorage.getItem('vera.widget.as.' + key(rec)) || ''; } catch (_) { this._viewAs = ''; } }
      const form = (this._viewAs && viewsFor(rec, form0, this._data).includes(this._viewAs)) ? this._viewAs : form0;
      const opts = { record: rec, draw: rec.draw, title: rec.title, panel: rec.panel, base: this.base, kids: this._kids || {}, ui: this._ui, height: this._bodyH || undefined, width: this._bodyW || undefined, projection: rec.projection, textK: textKOf(this) };   // L and XL compose around the form
      // nothing read yet — no source, a source that waits for a click, a read in flight, a read that failed — draws the
      // form's SAMPLE face, marked, and says why in the caption; the widget always has a face (never "no data yet")
      const wasRead = !!this._read, dataM = mapped(rec, form, this._data), have = this._data !== undefined && !isEmpty(dataFor(dataM, form));   // what the form would draw of the answer
      const sampled = !wasRead && !have && form !== 'panel' && form !== 'composite' && form !== 'element';   // an element draws itself: it is never a sample      // the sample face: no source, or never read
      const readEmpty = wasRead && !have && !this._err, stale = wasRead && !!this._err && have;
      // the face's STATE, on the host (data-state) and in widget:rendered: reading (a read is on its way - the sample face
      // is drawn faint, never as though it were the reading) · failed (a read that never answered) · sample (nothing to
      // read) · empty · stale · live. A bare host (a dashboard tile) says it in its own head, so its body carries no tag.
      const reading = sampled && !this._err && !this._empty && !!rec.source && readable(rec.source) && this._data === undefined;
      const state = reading ? 'reading' : (this._err && !have ? 'failed' : (sampled ? 'sample' : (readEmpty ? 'empty' : (stale ? 'stale' : 'live'))));
      if (this.getAttribute('data-state') !== state) this.setAttribute('data-state', state);
      opts.sampleTag = this.hasAttribute('bare') ? false : (reading ? 'reading…' : (state === 'failed' ? 'read failed' : undefined));
      let why = '';
      if (this._err) why = esc(rec.source + ': ' + this._err) + (have ? ' · last reading' : ' · sample');
      else if (this._empty && !have) why = esc(rec.source) + ' · read empty · sample';
      else if (readEmpty) why = 'read · empty';
      else if (this._data === undefined && rec.source && !readable(rec.source)) why = '<button class="vw-read" data-read>Read ' + esc(rec.source) + '</button>';
      else if (this._data === undefined && rec.source) why = 'reading ' + esc(rec.source) + '…';
      // a read that answered with nothing says so in one voice; everything else draws as it did
      const body = readEmpty && form !== 'panel' && form !== 'composite'
        ? NODATA(rec.source, size)
        : draw(form, have ? this._data : undefined, size, Object.assign({}, opts, wasRead ? { sample: false } : {}));
      // bare: the HOST draws the head (a dashboard tile's own head carries the title and the record chip — the board's
      // tile is one head over the body), so the element draws its body and caption alone, as xs and s already do
      const small = size === 'xs' || size === 's' || this.hasAttribute('bare');
      const figureTxt = figure(form, sampled ? sample(form) : dataM);
      // the record's skin dresses the element with the page's own pack rules (data-style is what themes.css keys on);
      // motion off holds every moving form still
      const skin = rec.skin && rec.skin !== 'inherit' ? rec.skin : '';
      if (skin) this.setAttribute('data-style', skin); else if (this._skinned) this.removeAttribute('data-style');
      this._skinned = !!skin;
      const cap = (why ? why + ' · ' : '') + (rec.read.window ? 'window ' + esc(rec.read.window) : (rec.source ? esc(rec.source) : (rec.panel ? 'panel ' + esc(rec.panel) : (sampled ? 'sample · no source' : ''))));
      const acts = size === 'xl' ? '<div class="vw-acts">' + rec.actions.filter((a) => ACTIONS[a]).map((a) => '<button data-act="' + a + '">' + ACTIONS[a] + '</button>').join('') + '</div>' : '';
      const was = (this._twKey === key(rec) && rec.frame.motion !== false) ? motionBefore(this._sh) : null; this._twKey = key(rec);
      this._sh.innerHTML = '<style>' + ELEMENT_CSS + CSS + TIP_CSS_S + '</style><div class="vw-root" data-form="' + esc(form) + '" data-size="' + size + '"' + (sampled ? ' data-sample="1"' : '') + (readEmpty ? ' data-empty="1"' : '') + (stale ? ' data-stale="1"' : '') + (rec.frame.motion === false ? ' data-motion="0"' : '') + (rec.frame.legend ? ' data-legend="1"' : '') + '>'
        + (small ? body : '<div class="vw-hd"><i></i>' + esc(rec.title) + (figureTxt && (form === 'radial' || form === 'counter' || form === 'bar' || form === 'trace') ? '<b>' + figureTxt + '</b>' : '') + '</div><div class="vw-body">' + body + '</div>'
          + '<div class="vw-cap">' + cap + '<span class="sp"></span>' + (form !== form0 ? 'viewed as ' + esc(form) + ' · ' : '') + (this._drawn && this._drawn !== rec.form ? 'drawn as ' + esc(this._drawn) + ' · ' : '') + esc(rec.form) + ' · ' + size + '</div>' + acts)
        + '</div>';
      const rb = this._sh.querySelector('[data-read]'); if (rb) rb.addEventListener('click', () => this.read(true));
      this._sh.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => this._act(b.dataset.act)));
      const setUi = (kv) => { const i = kv.indexOf(':'); if (i < 0) return; const k = kv.slice(0, i), v = kv.slice(i + 1); this._ui[k] = /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v; };
      this._sh.querySelectorAll('[data-vb-set]').forEach((b) => b.addEventListener('click', (ev) => { ev.stopPropagation(); setUi(b.dataset.vbSet); if (b.dataset.vbSet2) setUi(b.dataset.vbSet2); if (/^(off|view):/.test(b.dataset.vbSet) && /^(month|schedule|calnav|agenda)$/.test(canon(this._rec.form))) this._calMove(b.dataset.vbSet); else this.render(); }));
      this._sh.querySelectorAll('[data-vb-input]').forEach((i) => i.addEventListener('input', () => { this._ui[i.dataset.vbInput] = /^-?\d+(\.\d+)?$/.test(i.value) ? Number(i.value) : i.value; this.render(); }));
      this._sh.querySelectorAll('[data-vb-link]').forEach((a) => a.addEventListener('click', (ev) => { ev.preventDefault(); this.dispatchEvent(new CustomEvent('widget:open', { bubbles: true, composed: true, detail: { record: this._rec, href: a.dataset.vbLink, key: key(this._rec) } })); }));
      hydrate(this._sh);
      this._viewAsWire(form0, form, rec);
      motionAfter(this._sh, was);
      if (canon(form) === 'vgraph' && (have || (rec.draw && rec.draw.layer))) mountVeraGraph(this, rec, dataM, size); else if (this._vg || this._vgHost) unmountVeraGraph(this);
      // a Loop Lab element placed as a widget: mounted into the slot from M up (XS/S say what it is)
      if (canon(form) === 'element' && size !== 'xs' && size !== 's') mountElement(this, rec); else if (this._elHost) { this._elHost.remove(); this._elHost = null; this._elSig = ''; }
      // the entry motion plays once, on the first real reading (a refresh redraws without it)
      if (state === 'live' && !this.hasAttribute('data-entered') && !this._entering) { this._entering = true; setTimeout(() => { this.setAttribute('data-entered', ''); this._entering = false; }, 900); }
      // a bare element (a dashboard tile's body) measures too: its host sizes it, and the forms fit what they are given
      if (size !== 'xs' && size !== 's' && this._measured !== size) { const b = this._sh.querySelector('.vw-body') || (this.hasAttribute('bare') ? (this._sh.querySelector('.vw-root') || this) : null); const hb = b ? b.clientHeight : 0, wb = b ? b.clientWidth : 0;   /* bare: the root is the box the content has */ if (hb > 0 || wb > 0) this._measured = size; if ((hb > 48 && Math.abs(hb - (this._bodyH || 0)) > 12) || (wb > 80 && Math.abs(wb - (this._bodyW || 0)) > 12)) { if (hb > 48) this._bodyH = hb; if (wb > 80) this._bodyW = wb; this.render(); return; } }
      this.dispatchEvent(new CustomEvent('widget:rendered', { bubbles: true, composed: true, detail: { form, size, sample: sampled, empty: readEmpty, stale } }));   // the state rides on the host: data-state
    }
    /* ── THE FACE'S OWN WIDTH, when it has one ────────────────────────────────────────────────────────────────
       A chip face (xs · s) is an inline-flex, nowrap thing: it is exactly as wide as its glyph and its figure and
       no wider. A host that hands it a share of a column therefore leaves the rest of that share blank — which is
       what a sticker-sized widget on the session canvas looked like (owner, 2026-09-24: "canvas items that are
       smaller than a column have large blank areas i.e. widgets").
       Every other face is width:100% by design and answers 0, which means "as wide as you like". Read only:
       nothing here changes what is drawn. ───────────────────────────────────────────────────────────────────── */
    naturalWidth() {
      const r = this._sh; if (!r) return 0;
      const f = r.querySelector('.vw-root > .vw-xs, .vw-root > .vw-chip'); if (!f) return 0;
      const w = Math.ceil((f.getBoundingClientRect && f.getBoundingClientRect().width) || f.scrollWidth || 0);
      return w > 0 ? w : 0;
    }
  }
  if (window.customElements && !customElements.get('vera-widget')) customElements.define('vera-widget', VeraWidgetEl);
  window.VeraWidget = { draw, forms, normalise, formByShape, dataFor, applyMap, pick, mapped, formFor, readable, key, hydrate, sample, call, css: () => CSS, ensureCss, ensureIso, figure, sizes: SIZES.slice(), heights: Object.assign({}, HEIGHT), sizeForWidth, shapeFields: SHAPE_FIELDS, version: 5 };
  Object.assign(window.VeraWidget, { dive, openBlock, rowTip, rowRef, fontScale, fromCapResult, fromCapStream, capForms: () => CAP_FORMS.slice(), capHints: () => Object.assign({}, CAP_HINTS),
    drawer, relatedTo, resolveArgs, toVeraGraph, itemAt, ensureVeraGraph });   // round 3: the data drawer, the calendar's arguments, the Vera graph form   // the widget review, round 2: blocks, the deep dive, the text-size setting

  // the image studio's readers: the picture a row is, the animations a record carries, the drawer's picture
  Object.assign(window.VeraWidget, { studio: { imgOf, spriteAnims, drMedia } });

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
  const CFG_CSS = fontScale(`
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
.vwc-ss{padding:2px 0 4px}.vwc-ss input{width:100%;background:var(--bg,var(--bg0,#0e0f12));border-radius:var(--r-sm,6px);padding:4px 8px}
.vwc-src.other{opacity:.75}.vwc-src.other::after{content:attr(data-shape);margin-left:5px;font-size:7.5px;opacity:.7;letter-spacing:.04em}
.vwc-srcd{display:flex;flex-direction:column;gap:2px;padding:0 0 6px;max-height:190px;overflow:auto;scrollbar-width:thin}.vwc-srcg summary{cursor:pointer;font-size:9px;text-transform:uppercase;letter-spacing:.07em;font-weight:600;color:var(--t3,var(--dim,#6b7280));display:flex;gap:6px;align-items:baseline;padding:3px 0;list-style:none}.vwc-srcg summary::-webkit-details-marker{display:none}.vwc-srcg summary::before{content:'▸';font-size:8px;opacity:.7}.vwc-srcg[open] summary::before{content:'▾'}.vwc-srcg summary b{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-weight:400;font-size:8.5px}.vwc-srcg .vwc-srcl{max-height:72px;padding-left:10px}
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
.vwc-pvf.m{width:302px;height:200px}.vwc-pvf.l{width:100%;height:200px}.vwc-pvf.xl{width:100%;height:300px}
/* the frame is the size it is previewing and the widget FILLS it. It only ever asked for a minimum height and left the
   widget to find its own, which the widget cannot do since its figures size to their body: a meter came out 30px tall
   in a 200px frame — a sliver of a widget in an empty frame, not a preview of it (Notes/42 defect 69). XS and S stay
   as they are: those previews are a phrase with the widget set in it, sized by the line. */
.vwc-pvf.m,.vwc-pvf.l,.vwc-pvf.xl{display:flex;flex-direction:column;box-sizing:border-box}
.vwc-pvf vera-widget{display:block;width:100%}
.vwc-pvf.m > vera-widget,.vwc-pvf.l > vera-widget,.vwc-pvf.xl > vera-widget{flex:1 1 auto;min-height:0}
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
.vwc-empty{color:var(--t3,var(--dim,#6b7280));font-size:10px;padding:10px 8px}`);
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
    try { const r = await call(st.base, 'widget.sources', { limit: 2000 }); if (st !== S) return; st.sources = (r && Array.isArray(r.sources)) ? r.sources : []; st.srcMeta = r && r.domains ? { domains: r.domains, total: r.total, tiers: r.tiers } : null; } catch (_) { if (st !== S) return; st.sources = []; }
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
    else { const shapes = SHAPE_ORDER.filter((s) => (!S.opts.shape || S.opts.shape === s) && (!S.srcShape || S.q || S.srcShape === s));   // a picked source narrows the forms to its shape (the search lifts it)
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
    const rec = S.rec; const was = rec.form; rec.form = id; rec.shape = shapeOf(rec); if (S.srcShape && S.srcShape !== rec.shape) S.srcShape = '';
    const f = formEntry(id); if (f) { if (!(f.proj || []).includes(rec.projection)) rec.projection = (f.proj || ['flat'])[0]; if (!(f.sizes || SIZES).includes(rec.frame.size)) rec.frame.size = f.sizes[f.sizes.length - 1]; }
    if (o && o.size) rec.frame.size = o.size;
    if (was !== id) { rec.draw = {}; rec.read.map = {}; if (rec.source && S.sources) { const s = S.sources.find((x) => x.id === rec.source); if (s && s.shape !== rec.shape) rec.source = ''; } }
    if (id === 'composite' && !Array.isArray(rec.children)) { rec.layout = '2x2'; rec.children = []; }
    if (!S.titleTouched) rec.title = '';
    changed(true);
  }
  function pickTemplate(t) { S.picked = { kind: 'tpl', id: t.id }; const keep = S.rec.placement; S.rec = recFrom(Object.assign({}, t, { template: t.id }), S.into); if (!S.rec.placement.length) S.rec.placement = keep; S.rec.shape = shapeOf(S.rec); S.titleTouched = true; if (!S.sizes.includes(S.rec.frame.size)) S.rec.frame.size = S.sizes.includes('m') ? 'm' : S.sizes[0]; changed(true); }
  // a pick: the record's source, its arguments (the required ones listed, the rest empty), the map the measurement suggests
  // (the container the rows sit in), the refresh floor; a source of ANOTHER shape moves the record to that shape — the
  // catalogue narrows to it and the form becomes that shape's first (the form list follows the source, defect 57)
  function pickSource(src) { const rec = S.rec; rec.source = src ? src.id : ''; rec.read.map = {}; rec.read.args = {};
    if (src) { (src.required || []).forEach((a) => { rec.read.args[a] = ''; }); if (src.map && typeof src.map === 'object') Object.keys(src.map).forEach((k) => { if (src.map[k] && src.map[k] !== '$') rec.read.map[k] = src.map[k]; else if (src.map[k] === '$') rec.read.map[k] = '$'; });
      if (!rec.read.refresh && src.refresh_min && src.refresh_min !== 'live' && src.refresh_min !== 'event') rec.read.refresh = src.refresh_min; if (!rec.draw.unit && src.unit) rec.draw.unit = src.unit;
      const shape = shapeOf(rec); if (src.shape && src.shape !== shape && !S.opts.shape) { S.srcShape = src.shape; const cat = (S.cat && S.cat.forms) || []; const f = cat.find((x) => x.shape === src.shape && isLive(x.id)) || cat.find((x) => x.shape === src.shape); if (f) { rec.form = f.id; rec.shape = src.shape; rec.draw = {}; const fe = formEntry(f.id); if (fe && !(fe.proj || []).includes(rec.projection)) rec.projection = (fe.proj || ['flat'])[0]; } } else S.srcShape = ''; }
    else S.srcShape = '';
    if (!S.titleTouched) rec.title = ''; changed(true); }

  /* ── every edit lands here: the record column re-renders (unless typing), the preview, the JSON, the host, the validator ── */
  const validateLater = dbg(() => validate(), 350);
  function changed(rerender) {
    if (!S) return; S.armed = false; S.rec.shape = shapeOf(S.rec);
    S.dirty = true;   // the record changed since the last validation was sent
    if (rerender) { renderRec(); renderCat(); renderPacks(); }
    renderPvw(); renderFoot(); validateLater();
    try { if (typeof S.opts.onChange === 'function') S.opts.onChange(recOut(S.rec)); } catch (_) {}
  }
  // what widget.validate said of a record: the problems that bar it, the warnings, the normalised record
  const verdict = (r) => { const probs = (r && Array.isArray(r.problems)) ? r.problems : [];
    // a record with no source draws its sample face and stays editable (widget_record.validate's own rule): a warning here, not a bar
    return { problems: probs.filter((p) => p !== 'no source'), warnings: ((r && Array.isArray(r.warnings)) ? r.warnings : []).concat(probs.filter((p) => p === 'no source').map(() => 'no source · the sample face until one is picked')), validated: (r && r.record && typeof r.record === 'object') ? r.record : null }; };
  async function validate() {
    const st = S; if (!st) return; const seq = (st.vseq = (st.vseq || 0) + 1);   // an older answer never overwrites a newer record's
    st.dirty = false;   // the record sent is the record as it stands; a change after this marks it dirty again
    try { const r = await call(st.base, 'widget.validate', { record: recOut(st.rec) }); if (st !== S || seq !== st.vseq) return; Object.assign(st, verdict(r)); }
    catch (_) { if (st !== S || seq !== st.vseq) return; st.problems = []; st.warnings = []; st.validated = null; }
    st.vAnswered = seq;
    renderFoot();
  }
  // widget.validate's normalised record with the sheet's own keys kept beside it (what OK hands back)
  function finalise(out, v) {
    const fin = v ? Object.assign({}, v, { title: out.title || v.title, read: Object.assign({}, v.read, { map: out.read.map, args: out.read.args, range: out.read.range || null }), skin: out.skin || v.skin, frame: Object.assign({}, v.frame, { dive: out.frame.dive, caption: out.frame.caption, legend: out.frame.legend, motion: out.frame.motion != null ? out.frame.motion : v.frame.motion }),
      draw: Object.assign({}, out.draw, v.draw), actions: out.actions, placement: out.placement, place: out.place, template: out.template, panel: v.panel || out.panel, projection: v.projection || out.projection, children: out.children || v.children }) : out;
    if (fin.data === undefined && out.data !== undefined) fin.data = out.data;
    return fin;
  }
  // The staged OK: the sheet has closed and the caller has placed the record as it stood; widget.validate finishes here
  // and the caller hears the verdict through opts.onValidated(record | null, problems, warnings) — the tile takes the
  // normalised record, or names the problems and stays editable.
  async function validateDetached(st, out) {
    let v; try { v = verdict(await call(st.base, 'widget.validate', { record: out })); } catch (e) { v = { problems: [], warnings: ['widget.validate could not be reached: ' + String(e && e.message || e).slice(0, 80)], validated: null }; }
    try { if (typeof st.opts.onValidated === 'function') st.opts.onValidated(v.problems.length ? null : finalise(out, v.validated), v.problems, v.warnings); } catch (_) {}
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
    const needsNone = rec.form === 'panel' || rec.form === 'composite' || rec.form === 'element';
    const so = sec('Source', 'anything of shape <b>' + esc(shape) + '</b>' + (S.sources ? '' : ' · loading…'));
    if (rec.form === 'panel') { row(so, 'Panel', inp(rec.panel || rec.source.replace(/^panel:/, ''), (v) => { rec.panel = v; rec.source = v ? 'panel:' + v : ''; changed(false); }, 'a registered panel id')); }
    else if (!needsNone) {
      const all = S.sources || []; const sq = String(S.sq || '').trim().toLowerCase(); const hit = (s) => !sq || (s.id + ' ' + (s.desc || '') + ' ' + (s.domain || '') + ' ' + s.shape).toLowerCase().indexOf(sq) >= 0;
      const mine = all.filter((s) => s.shape === shape && hit(s)), others = all.filter((s) => s.shape !== shape && hit(s));
      const chipOf = (s) => { const c = h('span', 'vwc-src' + (rec.source === s.id ? ' on' : '') + (s.shape !== shape ? ' other' : ''), s.id); c.setAttribute('data-shape', s.shape); c.title = (s.desc || s.note || '') + ' · ' + s.shape + (s.refresh_min ? ' · refresh ≥ ' + s.refresh_min : '') + (s.unit ? ' · ' + s.unit : '') + ((s.required || []).length ? ' · needs ' + s.required.join(', ') : '') + (s.tier ? ' · ' + s.tier : ''); c.addEventListener('click', () => pickSource(s)); return c; };
      // the search across every source, with the counts (the sheet's "N sources · M of this shape")
      const sb = h('div', 'vwc-ss'); const si = h('input', 'vwc-sq'); si.type = 'search'; si.placeholder = 'find a source · ' + all.length + ' sources · ' + all.filter((s) => s.shape === shape).length + ' of shape ' + shape; si.value = S.sq || ''; si.addEventListener('input', () => { S.sq = si.value; renderRec(); const q2 = S.el.querySelector('.vwc-sq'); if (q2) { q2.focus(); q2.setSelectionRange(q2.value.length, q2.value.length); } }); sb.appendChild(si); so.appendChild(sb);
      const chips = h('div', 'vwc-srcl'); const none = h('span', 'vwc-src' + (rec.source ? '' : ' on'), 'none · sample'); none.title = 'no source: the widget draws its sample face'; none.addEventListener('click', () => pickSource(null)); chips.appendChild(none);
      const shown = mine.slice(0, 120); const cur = mine.find((s) => s.id === rec.source); if (cur && !shown.includes(cur)) shown.unshift(cur);   // the record's own source leads, wherever it sorts
      shown.forEach((s) => chips.appendChild(chipOf(s)));
      if (mine.length > 120) { const more = h('span', 'vwc-dim', '+ ' + (mine.length - 120) + ' more of shape ' + shape + ' · type to find one'); more.title = 'the list shows the first 120; the search narrows it'; chips.appendChild(more); }
      if (rec.source && !all.some((s) => s.id === rec.source)) { const c = h('span', 'vwc-src on', rec.source); c.title = 'the record\'s source (not in the registry)'; chips.appendChild(c); }
      if (S.sources && !mine.length) chips.appendChild(h('span', 'vwc-dim', 'no ' + shape + ' source ' + (sq ? 'matches' : 'registered') + ' · another shape below, or type one:'));
      so.appendChild(chips);
      // every other domain, folded — a pick there changes the record's shape (the catalogue narrows to it)
      if (others.length) { const byD = {}; others.forEach((s) => { (byD[s.domain || 'Other'] = byD[s.domain || 'Other'] || []).push(s); });
        const fold = h('div', 'vwc-srcd'); Object.keys(byD).sort().forEach((d) => { const det = h('details', 'vwc-srcg'); if (sq) det.open = true; const sm = h('summary'); sm.appendChild(h('span', '', d)); sm.appendChild(h('b', '', String(byD[d].length))); det.appendChild(sm); const l = h('div', 'vwc-srcl'); byD[d].slice(0, 80).forEach((s) => l.appendChild(chipOf(s))); det.appendChild(l); fold.appendChild(det); }); so.appendChild(fold); }
      if (S.sources && !mine.length) row(so, 'Source', inp(rec.source, (v) => { rec.source = v; changed(false); }, 'capability id'));
      const src = all.find((s) => s.id === rec.source);
      if (src && (src.params || src.args || []).length) { (src.params && src.params.length ? src.params : (src.args || []).map((a) => ({ name: a }))).forEach((p) => { const r = row(so, p.name + (p.required ? ' *' : ''), inp(rec.read.args[p.name], (v) => { if (v === '') delete rec.read.args[p.name]; else rec.read.args[p.name] = v; changed(false); }, (p.type || 'arg') + (p.default != null && p.default !== '' ? ' · ' + p.default : ''))); if (p.desc) r.title = p.desc; }); }
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
    const recNow = JSON.stringify(out);
    if (!vw || !vw.isConnected || keyNow !== S.lastPv) {
      stage.innerHTML = ''; const fr = h('div', 'vwc-pvf ' + size); fr.setAttribute('data-w', 'preview · ' + size);
      if (window.customElements && customElements.get('vera-widget')) { vw = document.createElement('vera-widget'); vw.setAttribute('size', size); if (S.base) vw.setAttribute('base', S.base); vw.record = out;
        vw.addEventListener('widget:rendered', (ev) => { const d = (ev && ev.detail) || {}; note.textContent = d.sample ? 'sample · no source read' : (rec.source ? 'live · ' + rec.source : 'record only'); note.className = 'dim ' + (d.sample ? 'sample' : 'live'); });
        if (size === 'xs') { fr.appendChild(document.createTextNode('ct126 is serving qwen3:30b at ')); fr.appendChild(vw); fr.appendChild(document.createTextNode(' with 4 in flight and step 5 waiting on you.')); }
        else fr.appendChild(vw); }
      else { const n = h('div', 'vwc-pvn'); n.appendChild(h('span', 'big', rec.form)); n.appendChild(document.createTextNode('the widget renderer is not on this page')); fr.appendChild(n); vw = null; }
      stage.appendChild(fr); S.pv = vw; S.lastPv = keyNow; S.lastRec = recNow;
    } else if (recNow !== S.lastRec) {
      // the rest of the record — its actions, its shape, its arguments — is not in the key the frame is rebuilt for, so
      // those edits used to leave the preview showing the record it was built with. The same element simply takes the
      // new one (defect 69)
      S.lastRec = recNow; try { vw.record = out; } catch (_) {}
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
    const out = recOut(st.rec); if (!out.title) out.title = titleOf(st.rec);
    // a validation that has answered for the record as it stands decides now (problems arm OK once, as before); without
    // one the record is handed over at once and validated behind the closed sheet — OK never waits on widget.validate
    // (7–15 s on a busy backend; the tile used to wait that long to appear)
    const fresh = !st.dirty && st.vAnswered && st.vAnswered === st.vseq;
    if (!fresh) {
      try { if (typeof st.opts.onValidating === 'function') st.opts.onValidating(); } catch (_) {}
      validateDetached(st, out); cfgClose(out); return;
    }
    if (st.problems.length && !st.armed) { st.armed = true; renderFoot(); return; }
    cfgClose(finalise(out, st.validated));
  }
  window.VeraWidgetConfig = { open: cfgOpen, close: () => cfgClose(null), recordFrom: recFrom, recordOut: recOut, entries: CG_ENTRIES.slice(), packs: PACKS.slice(), version: 3 };
})();
