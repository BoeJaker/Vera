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
     window.VeraWidget.formFor(record, data)           → the form that can draw THIS data (the chosen one, else the shape's, else kv)
     window.VeraWidget.readable(cap)                   → may a block read this capability on its own?
     window.VeraWidget.key(record)                     → the record's identity: form · source · args
     window.VeraWidget.hydrate(root)                   → mounts <vera-mermaid> into the pipes form's slots when it is defined
     <vera-widget record='{…json…}' size="m|auto" base="">   .record (property) · .refresh() · .read()
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
                  node: 'kv', form: 'kv', tank: 'radial', turbine: 'counter', rate: 'counter', candles: 'trace', orbit: 'list', shelf: 'list', stack: 'list', city: 'table', iso: 'table', globe: 'scatter' };
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
    }
    return x;
  }
  const series = (d) => (Array.isArray(d) ? d : []).map((p) => typeof p === 'number' ? p : num(p && (p.v ?? p.value ?? p.y ?? p.close)));
  const keyed = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? Object.keys(d).filter((k) => typeof d[k] === 'number').map((k) => [k, d[k]]) : (Array.isArray(d) ? d.filter((r) => r && typeof r === 'object' && ['v', 'value', 'n', 'count'].some((k) => typeof r[k] === 'number')).map((r) => [String(r.name ?? r.k ?? r.key ?? r.label ?? ''), num(r.v ?? r.value ?? r.n ?? r.count)]).filter((kv) => kv[0]) : []);
  const rows = (d) => (Array.isArray(d) ? d : []).filter((r) => r && typeof r === 'object');
  const level = (d) => (d && typeof d === 'object' && !Array.isArray(d)) ? { v: num(d.value), lo: num(d.min ?? 0), hi: num(d.max ?? 100), unit: typeof d.unit === 'string' ? d.unit : '', delta: d.delta } : (typeof d === 'number' ? { v: d, lo: 0, hi: 100, unit: '' } : null);
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
  R.context_graph = (d, H, opts) => {
    opts = opts || {}; const nodes = ((d && d.nodes) || []).filter((n) => n && (n.id || n.label)).slice(0, 60); const rels = ((d && (d.rels || d.links || d.edges)) || []).slice(0, 120);
    if (!nodes.length) return EMPTY('no context yet');
    if (opts.size === 'xl' && opts.full !== false) return '<div class="vw-cgfull" data-cg="' + esc(JSON.stringify({ nodes, rels })) + '" style="position:relative;height:100%;min-height:' + Math.max(H, 220) + 'px"><small class="wempty">context graph · ' + nodes.length + ' records</small></div>';
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
  };
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
    const kids = (o && o.record && Array.isArray(o.record.children)) ? o.record.children : [];
    if (!kids.length) return EMPTY('a composite needs children');
    const layout = (o && o.record && o.record.layout) || 'grid';
    return '<div class="vw-comp vw-comp-' + esc(layout) + '">' + kids.slice(0, 8).map((c) => { const rec = c && typeof c.record === 'object' ? c.record : null; if (!rec) return '<div class="vw-slot"><small>' + esc(String(c && c.record || '')) + '</small></div>'; const n = normalise(rec); return '<div class="vw-slot" data-slot="' + esc(c.slot || '') + '">' + draw(n.form, rec.data, layout === 'rail' ? 's' : 'm', { record: n, draw: n.draw, title: n.title }) + '</div>'; }).join('') + '</div>';
  };

  /* ── draw at a size: the composition around the form ─────────────────── */
  const GLYPH = { context_graph: '◎', trace: '∿', radial: '◔', counter: '123', bar: '▬', bars: '▥', thermo: '≣', heat: '▦', matrix: '▦', donut: '◑', stack: '▤', pills: '◦', log: '≡', lane: '≡', table: '▦', files: '⊞', list: '≡', checklist: '☑', stepper: '⋮', calendar: '▦', string: '¶', kv: '≔', pipes: '⌥', scatter: '⁘', panel: '▭', composite: '⊞' };
  function draw(form, data, size, opts) {
    opts = opts || {}; const f0 = String(form || ''); const f = canon(f0); size = SIZES.includes(size) ? size : 'm';
    const H = opts.height || HEIGHT[size] || 70;
    if (!R[f]) return EMPTY('form ' + f0 + ' · no drawing yet');
    const d = dataFor(data, f);
    if (size === 'xs') return '<span class="vw-xs" title="' + esc(opts.title || f0) + '"><i>' + (GLYPH[f] || '▢') + '</i>' + (figure(f, data) || '—') + '</span>';
    if (size === 's') return '<span class="vw-chip" title="' + esc(opts.title || f0) + '"><i>' + (GLYPH[f] || '▢') + '</i><b>' + (figure(f, data) || '—') + '</b>' + (opts.title ? '<small>' + esc(opts.title) + '</small>' : '') + '</span>';
    if (d == null && f !== 'panel' && f !== 'composite') return EMPTY('no data yet');
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
      read: { refresh: String(o.refresh || read.refresh || reads.every || ''), window: String(o.window || read.window || ''), args },
      frame: { size: SIZES.includes(size) ? size : 'm' }, draw, panel, actions: Array.isArray(o.actions) ? o.actions : ['dive', 'pin', 'ask'],
      children: Array.isArray(o.children) ? o.children : undefined, layout: o.layout, data: o.data };
  }
  const readable = (cap) => /(\.(get|list|status|load|history|read|stats|metrics|recent|tail|search|find|show|info|summary|query|health|state|series|events|nodes|jobs|runs|snapshot|top)|^obs\.|^sysmon\.|^perf\.|^nodes\.|^docker\.(ps|stats)|^git\.log|^markets\.)/.test(cap) && !/(write|delete|remove|create|run|exec|kill|restart|stop|start|set|save|send|post|push|upsert)\b/.test(cap);
  const key = (rec) => { const n = normalise(rec); return n.form + ' ' + (n.source || (n.panel ? 'panel:' + n.panel : '')) + ' ' + JSON.stringify(n.read.args || {}); };
  // the form that can draw THIS data: the chosen one, else what its shape picks, else the key · value list
  function formFor(rec, data) {
    const n = normalise(rec); const empty = (f) => { try { return /class="wempty"/.test(draw(f, data, 'm', { bare: true })); } catch (_) { return true; } };
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
      try { el.setContext((d.nodes || []).map((x) => Object.assign({ source: x.source || x.lane || 'context' }, x)), (d.rels || d.edges || []).map((e) => ({ from: e.from, to: e.to, label: e.kind || e.label || '' })), { focus: (d.nodes || []).filter((x) => x.included !== false).map((x) => x.id) }); } catch (_) {} n++; });
    const slots = R0.querySelectorAll ? R0.querySelectorAll('.vw-mm[data-mm-code]:not([data-live])') : [];
    if (!slots.length || !(window.customElements && customElements.get('vera-mermaid'))) return n;
    slots.forEach((slot) => { slot.dataset.live = '1'; const el = document.createElement('vera-mermaid'); el.setAttribute('bare', ''); el.setAttribute('fill', ''); slot.innerHTML = ''; slot.appendChild(el); try { el.render(slot.dataset.mmCode); } catch (_) {} n++; });
    return n;
  }

  /* ── the styles (host-injected once; the element carries them in its shadow) ── */
  const CSS = `
.wempty{color:var(--dim2,#8a92a0);font-size:9.5px;font-family:var(--mono,ui-monospace,monospace)}
.vw-svg{display:block;width:100%}
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
.vw-comp{display:grid;gap:8px;width:100%;grid-template-columns:1fr 1fr}.vw-comp-rows,.vw-comp-report{grid-template-columns:1fr}.vw-comp-rail{grid-template-columns:1fr}.vw-slot{min-width:0}
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
:host([size="xs"]) .vw-root,:host([size="s"]) .vw-root{display:inline-flex}:host([size="xs"]),:host([size="s"]){display:inline-block}`;
  const ACTIONS = { dive: 'Deep dive', pin: 'Pin to canvas', ask: 'Ask Vera', ops: 'Open in Ops', print: 'Print card', mute: 'Mute' };
  const REFRESH_FLOOR = 10;
  const parseRefresh = (s) => { const m = String(s || '').match(/^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$/); if (!m) return 0; const n = parseFloat(m[1]); return m[2] === 'ms' ? n / 1000 : m[2] === 'm' ? n * 60 : m[2] === 'h' ? n * 3600 : n; };
  const sizeForWidth = (w) => w <= 120 ? 'xs' : w <= 220 ? 's' : w <= 380 ? 'm' : w <= 620 ? 'l' : 'xl';

  class VeraWidgetEl extends HTMLElement {
    constructor() { super(); this._sh = this.attachShadow({ mode: 'open' }); this._rec = null; this._data = undefined; this._drawn = ''; this._timer = null; this._ro = null; this._auto = 'm'; }
    static get observedAttributes() { return ['record', 'size', 'base', 'template-id']; }
    get record() { return this._rec; }
    set record(v) { this._rec = normalise(v); this._data = (v && v.data !== undefined) ? v.data : undefined; this._drawn = ''; if (this.isConnected) this._boot(); }
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
    async _call(name, args) {
      const r = await fetch(this.base + '/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, arguments: args || {} }) });
      const j = await r.json(); return (j && j.type === 'tool_result') ? j.content : (j && j.content !== undefined ? j.content : j);
    }
    _boot() {
      if (!this._rec) { this._rec = normalise({}); }
      this.render();
      const cap = this._rec.source;
      if (this._data === undefined && cap && readable(cap)) this.read();
      const every = Math.max(REFRESH_FLOOR, parseRefresh(this._rec.read.refresh));
      if (this._timer) clearInterval(this._timer);
      if (cap && this._rec.read.refresh && readable(cap)) this._timer = setInterval(() => this.read(), every * 1000);
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
      const opts = { record: rec, draw: rec.draw, title: rec.title, panel: rec.panel, base: this.base };   // L and XL compose around the form
      let body;
      if (this._data === undefined && rec.source && !readable(rec.source)) body = '<button class="vw-read" data-read>Read ' + esc(rec.source) + '</button>';
      else if (this._err) body = EMPTY(rec.source + ': ' + this._err);
      else if (this._data === undefined && rec.source && rec.form !== 'panel' && rec.form !== 'composite') body = EMPTY('reading ' + rec.source + '…');
      else body = draw(form, this._data, size, opts);
      const small = size === 'xs' || size === 's';
      const figureTxt = figure(form, this._data);
      const cap = rec.read.window ? 'window ' + esc(rec.read.window) : (rec.source ? esc(rec.source) : (rec.panel ? 'panel ' + esc(rec.panel) : ''));
      const acts = size === 'xl' ? '<div class="vw-acts">' + rec.actions.filter((a) => ACTIONS[a]).map((a) => '<button data-act="' + a + '">' + ACTIONS[a] + '</button>').join('') + '</div>' : '';
      this._sh.innerHTML = '<style>' + ELEMENT_CSS + CSS + '</style><div class="vw-root" data-form="' + esc(form) + '" data-size="' + size + '">'
        + (small ? body : '<div class="vw-hd"><i></i>' + esc(rec.title) + (figureTxt && (form === 'radial' || form === 'counter' || form === 'bar' || form === 'trace') ? '<b>' + figureTxt + '</b>' : '') + '</div><div class="vw-body">' + body + '</div>'
          + '<div class="vw-cap">' + cap + '<span class="sp"></span>' + (this._drawn && this._drawn !== rec.form ? 'drawn as ' + esc(this._drawn) + ' · ' : '') + esc(rec.form) + ' · ' + size + '</div>' + acts)
        + '</div>';
      const rb = this._sh.querySelector('[data-read]'); if (rb) rb.addEventListener('click', () => this.read(true));
      this._sh.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => this._act(b.dataset.act)));
      hydrate(this._sh);
      this.dispatchEvent(new CustomEvent('widget:rendered', { bubbles: true, composed: true, detail: { form, size } }));
    }
  }
  if (window.customElements && !customElements.get('vera-widget')) customElements.define('vera-widget', VeraWidgetEl);
  window.VeraWidget = { draw, forms, normalise, formByShape, dataFor, formFor, readable, key, hydrate, css: () => CSS, ensureCss, figure, sizes: SIZES.slice(), heights: Object.assign({}, HEIGHT), sizeForWidth, version: 1 };
})();
