/* The Desk Lab (framework step F3; shared-planning/context-and-ui/DESK-DESIGN-FRAMEWORK-PLAN.md section 5.1).
   A page that draws widget forms side by side on fixtures, at every size and state, in either theme, with a performance
   readout - so a design is judged by measurement, not by eye alone. Driven by its controls or by its URL, so a probe can
   script it and read window.__deskLab:

     /ui/desk-lab?forms=stepper,heat&sizes=s,m,l&states=live,empty,failed&theme=dark&views=1&scenario=sweep

   forms    comma list, a family word (series, parts, rows, events, stages, graph, value, iso) or "all"
   sizes    xs,s,m,l,xl                 states  live,sample,reading,empty,failed,stale
   views    1 = also draw each form's view-as alternatives (the "variants" of a design session)
   data     sample (the form's own sample) | json (the pasted fixture)
   scenario sweep (pointer over every cell, frame times measured) | none

   The pure parts (URL parsing, budgets, percentiles, scenario steps) are exported for Node tests
   (tests/test_desk_lab.cjs). Block comments only (generated-code rule). */
(function (root) {
  'use strict';
  const SIZES = ['xs', 's', 'm', 'l', 'xl'];
  const STATES = ['live', 'sample', 'reading', 'empty', 'failed', 'stale'];
  /* the box each size is given (a dashboard tile's footprint at that size) */
  const BOX = { xs: [170, 44], s: [230, 76], m: [330, 156], l: [450, 236], xl: [660, 380] };
  /* the budget (DESK-DESIGN-FRAMEWORK-PLAN section 6): first draw, DOM size, frame time. A 60 Hz display's frame
     interval is itself 16.7 ms, so the p95 limit is one frame plus jitter (20 ms); a dropped frame reads 33 ms. */
  const BUDGET = { drawMs: { xs: 50, s: 50, m: 50, l: 120, xl: 120 }, dom: 1500, frameP95: 20 };
  const FAMILIES = {
    value: ['radial', 'ring', 'dial', 'counter', 'bar', 'meter', 'level', 'hero', 'gauge', 'tank', 'thermo', 'dots', 'glance', 'numbers', 'turbine', 'threshold', 'slider'],
    series: ['trace', 'area', 'step', 'slope', 'horizon', 'bump', 'small-multiples', 'spark-table', 'lines', 'trend-layers', 'candles', 'scope', 'histogram', 'column'],
    parts: ['donut', 'stack', 'stacked-bar', 'treemap', 'waffle', 'pareto', 'waterfall', 'funnel', 'stacks', 'rings', 'radar'],
    ranked: ['bars', 'ranked', 'lollipop', 'diverging', 'compare', 'bullet'],
    matrix: ['heat', 'matrix', 'cellmap', 'tabs', 'calendar'],
    rows: ['table', 'rows', 'list', 'cards', 'files', 'reading', 'feed', 'links', 'people', 'gallery', 'images', 'library', 'pages', 'devices', 'datasets', 'hosts', 'containers', 'models', 'sandboxes', 'notices', 'shelf', 'racks', 'carousel', 'temps', 'pills', 'checklist'],
    events: ['log', 'lane', 'timeline', 'activity', 'pulse', 'comet', 'sweep', 'agenda', 'schedule', 'month', 'calnav', 'gantt'],
    stages: ['stepper', 'pipeline', 'conveyor', 'approvals', 'progress', 'run-track'],
    graph: ['graph', 'flow', 'minigraph', 'pipes', 'topology', 'city', 'orbit'],
    text: ['markdown', 'string', 'announcement', 'split-flap', 'json', 'code', 'diff', 'kv', 'form', 'terminal', 'notebook', 'status', 'error'],
  };

  /* ── pure parts ── */
  const list = (v, all) => { const s = String(v == null ? '' : v).trim(); if (!s) return []; return s.split(',').map((x) => x.trim()).filter(Boolean); };
  function parseQuery(search, known) {
    const q = new URLSearchParams(String(search || '').replace(/^\?/, ''));
    let forms = [];
    list(q.get('forms') || 'stepper').forEach((f) => {
      if (f === 'all') forms = forms.concat(known || []);
      else if (FAMILIES[f]) forms = forms.concat(FAMILIES[f]);
      else if (f.endsWith('@iso') || !known || known.includes(f)) forms.push(f);
      else if (f === 'iso') forms = forms.concat((known || []).filter((k) => k.endsWith('@iso')));
    });
    forms = forms.filter((f, i) => forms.indexOf(f) === i && (!known || known.includes(f)));
    const sizes = list(q.get('sizes') || 's,m,l').filter((s) => SIZES.includes(s));
    const states = list(q.get('states') || 'live').filter((s) => STATES.includes(s));
    return { forms, sizes: sizes.length ? sizes : ['m'], states: states.length ? states : ['live'], theme: q.get('theme') === 'light' ? 'light' : 'dark',
      views: q.get('views') === '1', data: q.get('data') === 'json' ? 'json' : 'sample', scenario: q.get('scenario') === 'sweep' ? 'sweep' : 'none' };
  }
  function toQuery(o) {
    const p = new URLSearchParams();
    p.set('forms', (o.forms || []).join(',')); p.set('sizes', (o.sizes || []).join(',')); p.set('states', (o.states || []).join(','));
    if (o.theme === 'light') p.set('theme', 'light'); if (o.views) p.set('views', '1'); if (o.data === 'json') p.set('data', 'json'); if (o.scenario === 'sweep') p.set('scenario', 'sweep');
    return '?' + p.toString();
  }
  function percentile(xs, p) { const a = (xs || []).filter((x) => isFinite(x)).slice().sort((x, y) => x - y); if (!a.length) return 0; const i = Math.min(a.length - 1, Math.max(0, Math.ceil(p / 100 * a.length) - 1)); return a[i]; }
  function overBudget(cell) {
    const why = [];
    if (cell.drawMs > (BUDGET.drawMs[cell.size] || 50)) why.push('draw ' + cell.drawMs.toFixed(1) + ' ms > ' + BUDGET.drawMs[cell.size]);
    if (cell.dom > BUDGET.dom) why.push('dom ' + cell.dom + ' > ' + BUDGET.dom);
    if (cell.error) why.push('error: ' + cell.error);
    return why;
  }
  function summarise(cells, frames) {
    const draw = cells.map((c) => c.drawMs);
    return { cells: cells.length, over: cells.filter((c) => overBudget(c).length).length, errors: cells.filter((c) => c.error).length,
      drawP50: +percentile(draw, 50).toFixed(2), drawP95: +percentile(draw, 95).toFixed(2), domMax: Math.max(0, ...cells.map((c) => c.dom)),
      frameP95: frames && frames.length ? +percentile(frames, 95).toFixed(2) : null, frameOver: frames && frames.length ? percentile(frames, 95) > BUDGET.frameP95 : false };
  }
  /* a scenario is a list of steps; "sweep" hovers each cell's centre for a few frames */
  function scenarioSteps(name, cellIds) { if (name !== 'sweep') return []; const out = []; (cellIds || []).forEach((id) => { out.push({ hover: id, frames: 6 }); }); return out; }

  /* how a state is made without the network: the element's own fields, set before it draws (the frame's state rules) */
  function stateRecord(form, state, data, size) {
    const base = { form, title: form + ' · ' + state, frame: { size } };
    if (state === 'sample') return base;
    if (state === 'reading') return Object.assign(base, { source: 'desklab.status' });
    if (state === 'empty') return Object.assign(base, { data: [] });
    if (state === 'failed') return base;
    return Object.assign(base, { data });
  }
  function applyState(el, state) {
    if (state === 'reading') { el._call = () => new Promise(() => {}); }
    if (state === 'empty') { el._read = true; }
    if (state === 'failed') { el._err = 'simulated failure (Desk Lab)'; el._read = false; }
    if (state === 'stale') { el._err = 'simulated failure (Desk Lab) - last reading shown'; el._read = true; }
  }

  const api = { SIZES, STATES, BOX, BUDGET, FAMILIES, list, parseQuery, toQuery, percentile, overBudget, summarise, scenarioSteps, stateRecord, applyState };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (!root || !root.document) return;
  root.VeraDeskLab = api;

  /* ── the page ── */
  const doc = root.document;
  const $ = (s) => doc.querySelector(s);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  let longTasks = 0;
  try { new PerformanceObserver((l) => { longTasks += l.getEntries().length; }).observe({ type: 'longtask', buffered: false }); } catch (_) {}

  function fixture(form, opts) {
    if (opts.data === 'json') { try { return JSON.parse($('#lab-json').value || 'null'); } catch (_) { return undefined; } }
    const W = root.VeraWidget; try { return W.sample(form); } catch (_) { return undefined; }
  }
  const nodes = (el) => (el.shadowRoot ? el.shadowRoot.querySelectorAll('*').length : 0);

  async function build(opts) {
    const W = root.VeraWidget; const grid = $('#lab-grid'); grid.innerHTML = '';
    doc.documentElement.setAttribute('data-theme', opts.theme);
    /* measure each element's first draw: time spent in its render() calls until it settles */
    const proto = customElements.get('vera-widget') && customElements.get('vera-widget').prototype;
    if (proto && !proto.__labTimed) { const r0 = proto.render; proto.render = function () { const t0 = performance.now(); try { return r0.apply(this, arguments); } finally { this.__labMs = (this.__labMs || 0) + (performance.now() - t0); this.__labRenders = (this.__labRenders || 0) + 1; } }; proto.__labTimed = true; }
    /* warm-up: one throwaway widget drawn and dropped before anything is measured - the page's first widget paid for
       the stylesheet and the iso library, which charged the first cell 40-60 ms that were not its own (2026-10-11) */
    if (!root.__labWarm) { root.__labWarm = true; const wu = doc.createElement('vera-widget'); wu.style.cssText = 'position:absolute;left:-9999px;top:0;width:300px;height:150px'; wu.setAttribute('size', 'm'); wu.record = { form: 'heat', data: { a: 1, b: 2 } }; doc.body.appendChild(wu); await new Promise((res) => setTimeout(res, 400)); wu.remove(); }
    const rows = [];
    for (const form of opts.forms) {
      const variants = [form];
      if (opts.views) { try { const d = fixture(form, opts); (W.viewsFor(W.normalise({ form }), form, d) || []).forEach((v) => { if (!variants.includes(v)) variants.push(v); }); } catch (_) {} }
      for (const v of variants) for (const state of opts.states) rows.push({ form: v, base: form, state });
    }
    const head = doc.createElement('div'); head.className = 'lab-row lab-head';
    head.innerHTML = '<div class="lab-lbl">form · state</div>' + opts.sizes.map((s) => '<div class="lab-col" style="width:' + BOX[s][0] + 'px">' + s + '</div>').join('');
    grid.appendChild(head);
    const cells = [];
    for (const r of rows) {
      const row = doc.createElement('div'); row.className = 'lab-row';
      row.innerHTML = '<div class="lab-lbl"><b>' + esc(r.form) + '</b>' + (r.form !== r.base ? '<small>view of ' + esc(r.base) + '</small>' : '') + '<em class="st-' + r.state + '">' + r.state + '</em></div>';
      for (const size of opts.sizes) {
        const id = 'c' + cells.length;
        const wrap = doc.createElement('div'); wrap.className = 'lab-cell'; wrap.id = id; wrap.style.width = BOX[size][0] + 'px';
        const el = doc.createElement('vera-widget');
        el.setAttribute('size', size); el.style.cssText = 'display:block;height:' + BOX[size][1] + 'px';
        const data = fixture(r.base, opts);
        el.record = stateRecord(r.form, r.state, data, size);
        applyState(el, r.state);
        const hud = doc.createElement('div'); hud.className = 'lab-hud';
        wrap.appendChild(el); wrap.appendChild(hud); row.appendChild(wrap);
        cells.push({ id, form: r.form, base: r.base, state: r.state, size, el, hud });
      }
      grid.appendChild(row);
    }
    await new Promise((res) => setTimeout(res, 700));
    const out = cells.map((c) => { let error = ''; const st = c.el.getAttribute('data-state') || '';
      const want = { live: 'live', sample: 'sample', reading: 'reading', empty: 'empty', failed: 'failed', stale: 'stale' }[c.state];
      if (want && st && st !== want && !(c.state === 'live' && st === 'sample')) error = 'state ' + st + ' (wanted ' + want + ')';
      const cell = { id: c.id, form: c.form, base: c.base, state: c.state, size: c.size, drawMs: +(c.el.__labMs || 0).toFixed(2), renders: c.el.__labRenders || 0, dom: nodes(c.el), shown: st, error };
      const why = overBudget(cell);
      c.hud.innerHTML = '<span>' + cell.drawMs.toFixed(1) + ' ms</span><span>' + cell.dom + ' nodes</span><span>' + cell.renders + '×</span>' + (why.length ? '<b title="' + esc(why.join('; ')) + '">over</b>' : '');
      c.hud.classList.toggle('bad', !!why.length);
      return cell; });
    let frames = [];
    if (opts.scenario === 'sweep') frames = await sweep(cells);
    const summary = Object.assign(summarise(out, frames), { longTasks, heapMB: performance.memory ? +(performance.memory.usedJSHeapSize / 1048576).toFixed(1) : null });
    $('#lab-sum').innerHTML = '<span>' + summary.cells + ' cells</span><span>draw p50 ' + summary.drawP50 + ' ms</span><span>p95 ' + summary.drawP95 + ' ms</span><span>DOM max ' + summary.domMax + '</span>'
      + (summary.frameP95 != null ? '<span class="' + (summary.frameOver ? 'bad' : '') + '">frame p95 ' + summary.frameP95 + ' ms</span>' : '')
      + '<span>long tasks ' + summary.longTasks + '</span>' + (summary.heapMB != null ? '<span>heap ' + summary.heapMB + ' MB</span>' : '')
      + '<span class="' + (summary.over ? 'bad' : 'ok') + '">' + summary.over + ' over budget</span>';
    root.__deskLab = { opts, cells: out, summary, done: true };
    return root.__deskLab;
  }

  /* the sweep: the pointer visits every cell; frame times are the gaps between animation frames while it hovers */
  async function sweep(cells) {
    const frames = []; let last = performance.now(); let on = true;
    const tick = (t) => { frames.push(t - last); last = t; if (on) requestAnimationFrame(tick); };
    requestAnimationFrame((t) => { last = t; requestAnimationFrame(tick); });
    for (const step of scenarioSteps('sweep', cells.map((c) => c.id))) {
      const c = cells.find((x) => x.id === step.hover); const r = c.el.getBoundingClientRect();
      const ev = { bubbles: true, composed: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
      const tgt = (c.el.shadowRoot && c.el.shadowRoot.elementFromPoint && c.el.shadowRoot.elementFromPoint(ev.clientX, ev.clientY)) || c.el;
      tgt.dispatchEvent(new MouseEvent('pointerover', ev)); tgt.dispatchEvent(new MouseEvent('mouseover', ev)); tgt.dispatchEvent(new MouseEvent('mousemove', ev));
      await new Promise((res) => { let n = 0; const f = () => { if (++n >= step.frames) res(); else requestAnimationFrame(f); }; requestAnimationFrame(f); });
      tgt.dispatchEvent(new MouseEvent('mouseout', ev));
    }
    on = false; return frames.slice(2);
  }

  function readControls() {
    const forms = $('#lab-forms').value; const pick = (name) => [...doc.querySelectorAll('input[name="' + name + '"]:checked')].map((i) => i.value);
    return parseQuery(toQuery({ forms: list(forms), sizes: pick('size'), states: pick('state'), theme: $('#lab-theme').value, views: $('#lab-views').checked, data: $('#lab-data').value, scenario: $('#lab-scn').value }), knownForms());
  }
  function writeControls(o) {
    $('#lab-forms').value = o.forms.join(',');
    doc.querySelectorAll('input[name="size"]').forEach((i) => { i.checked = o.sizes.includes(i.value); });
    doc.querySelectorAll('input[name="state"]').forEach((i) => { i.checked = o.states.includes(i.value); });
    $('#lab-theme').value = o.theme; $('#lab-views').checked = o.views; $('#lab-data').value = o.data; $('#lab-scn').value = o.scenario;
  }
  /* the drawable form names: VeraWidget.forms() lists {id, iso}; an iso twin is its own form (id@iso) */
  function knownForms() { const W = root.VeraWidget; if (!W || typeof W.forms !== 'function') return null; const out = []; W.forms().forEach((f) => { if (f && f.id && !f.as) { out.push(f.id); if (f.iso) out.push(f.id + '@iso'); } }); return out.filter((f, i) => out.indexOf(f) === i).sort(); }
  async function run() { const o = readControls(); try { history.replaceState(null, '', toQuery(o)); } catch (_) {} return build(o); }

  function boot() {
    const W = root.VeraWidget; if (!W) { $('#lab-grid').textContent = 'widget_element.js did not load'; return; }
    const known = knownForms() || [];
    $('#lab-known').innerHTML = Object.keys(FAMILIES).map((f) => '<button class="btn" data-fam="' + f + '">' + f + '</button>').join('') + '<button class="btn" data-fam="iso">iso</button>';
    doc.querySelectorAll('[data-fam]').forEach((b) => b.addEventListener('click', () => { $('#lab-forms').value = b.dataset.fam; run(); }));
    const dl = doc.createElement('datalist'); dl.id = 'lab-forms-list'; dl.innerHTML = known.map((f) => '<option value="' + esc(f) + '">').join(''); doc.body.appendChild(dl);
    writeControls(parseQuery(root.location.search, known));
    $('#lab-run').addEventListener('click', run);
    $('#lab-forms').addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
    run();
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot); else boot();
})(typeof window !== 'undefined' ? window : null);
