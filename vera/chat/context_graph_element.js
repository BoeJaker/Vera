/* The context graph — the chat's graph column (UI redesign: the Chat & canvas set's Graph board, "the LHM
   expanded: the Context menu grown into the full graph"; Notes/40 §7 · §One page). The records the aide
   assembled for the turn in focus, drawn as a real graph, not a list: angle is the source, distance is lower
   relevance, area is tokens, hollow is related-but-not-injected. The unified graph's four layouts, here —
   galaxy · iso (through the shared ISO projection when /ui/iso.js is present) · flow · time — with the loop
   as a step chain down the left, the plan along the top, the relations inside the graph (what cites what,
   which step read which record, which uninjected memory still relates), a record panel, pan/zoom and Fit.
   The chat feeds it (CTX_NODES/CTX_EDGES, the live loop's events, the goals) and draws the records' runs
   to the message in focus from the positions this element reports.

   <vera-context-graph>  API:
     setContext(nodes, edges, {focus:[ids], reads:{mid:[ids]}, stepReads:[[ids]], color:(source)=>css, turn})
     appendLoopEvent(ev) · setLoopEvents(evs) · setPlan(goals) · view(name) · fit() · positions() · state()
   events: vera:ctx:rendered {view, tokens, lit} · vera:ctx:pick {id} · vera:ctx:toggle {id} · vera:ctx:focus-turn {mid}
   window.VeraContextGraph = { compute, VIEWS, version } — compute() is pure (node-testable).             */
(function (root) {
  'use strict';
  const VIEWS = [['galaxy', 'Galaxy', 'angle is the source, distance is relevance'], ['iso', 'Iso', 'the galaxy on an isometric plate, relevance as height'],
    ['flow', 'Flow', 'a column per source, the most relevant on top'], ['time', 'Time', 'a column per turn that first read it, a lane per source']];
  const RAD = Math.PI / 180;
  const ORDER = ['vector', 'graph', 'fabric', 'web', 'news', 'ontology', 'cap', 'skill', 'run', 'related_qa', 'worldview', 'agent', 'entities', 'urls', 'both'];
  const DEF_COL = { vector: '#a78bfa', graph: '#fb923c', both: '#8fb87a', fabric: '#38bdf8', memory: '#5a9e8f', web: '#f59e0b', news: '#e879f9', cap: '#ec4899', run: '#60a5fa', skill: '#5a9e8f', ontology: '#c9955a', related_qa: '#e8a44c', worldview: '#2dd4bf', agent: '#888' };
  const tokOf = (n) => n.tok != null ? +n.tok : n.tokens != null ? +n.tokens : Math.max(12, Math.round(String(n.text || n.label || '').length / 4));
  const px = (v) => Math.round(v * 10) / 10;

  /* ── the layout, pure: state + size → everything the element draws ───────────────────────────────── */
  function compute(S, W, H) {
    const view = VIEWS.some((v) => v[0] === S.view) ? S.view : 'galaxy';
    const color = (s) => (S.color && S.color(s)) || DEF_COL[s] || '#8a7e70';
    const nodes = (S.nodes || []).filter((n) => n && n.id);
    const off = S.layersOff || new Set();
    const ghosts = S.related !== false;
    // the chat's other graphs, in this one: the loop lane falls back to the run's DAG steps when no loop is live;
    // the session's memory graph joins the memory arc (below); a family chip folds each away
    const loop = off.has('loop') ? [] : ((S.loop && S.loop.length) ? S.loop : (S.dag || [])), plan = off.has('plan') ? [] : (S.plan || []);
    const hasLanes = loop.length > 0 || plan.length > 0;
    const LANE_L = loop.length ? 118 : 0, LANE_T = plan.length ? 52 : 0;
    const PW = Math.max(200, W), PH = Math.max(160, H);
    // the plot proper: right of the loop lane, below the plan row
    const cxp = LANE_L + (PW - LANE_L) / 2, cyp = LANE_T + (PH - LANE_T) / 2;
    // memory: the context's own memory records (injected) plus the session's memory graph (the rail's Memory tab) —
    // a session record already in the prompt is drawn once, filled; one never injected is hollow
    const ctxIds = new Set(nodes.map((n) => n.id));
    const memCtx = nodes.filter((n) => n.source === 'memory').map((n) => Object.assign({}, n, { _fam: 'memory', _injected: n.included !== false }));
    // a session record that is in the prompt is drawn once, in its sector; the rest of the session sits hollow on the arc
    const memSess = off.has('memory') ? [] : (S.memory || []).filter((m) => m && m.id && !ctxIds.has(m.id)).map((m) => ({ id: m.id, label: (m.text || m.summary || m.capability || m.category || m.id || '').slice(0, 60), source: 'memory', type: m.record_type || m.type || 'memory', score: m.importance == null ? 0.5 : +m.importance, text: m.text || m.summary || '', included: false, rec: m, _fam: 'memory', _injected: false, _sess: true, created_at: m.created_at || '' }));
    const mem = memCtx.concat(memSess);
    const ctx = nodes.filter((n) => n.source !== 'memory' && !off.has(n.source) && (ghosts || n.included !== false));
    const RMAX = Math.max(60, Math.min(PW - LANE_L, PH - LANE_T) / 2 - (mem.length ? 96 : 62));
    const srcs = [...new Set(ctx.map((n) => n.source || '?'))].sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
    const step = 360 / Math.max(srcs.length, 1);
    const focus = new Set(S.focus || []);
    const reads = S.reads || {};
    const readBy = (id) => Object.keys(reads).filter((k) => (reads[k] || []).indexOf(id) >= 0);
    const stepReads = S.stepReads || [];
    const readBySteps = (id) => stepReads.map((r, i) => (r || []).indexOf(id) >= 0 ? i + 1 : 0).filter(Boolean);
    const turnKeys = Object.keys(reads);
    const firstRead = (id) => { const ks = readBy(id); return ks.length ? Math.min.apply(null, ks.map((k) => turnKeys.indexOf(k) + 1)) : 0; };
    const isoP = S.isoProj || ((x, y, z) => { const u = x - cxp, v = y - cyp; return { x: cxp + (u - v) * 0.78, y: cyp + 40 + (u + v) * 0.39 - (z || 0) }; });
    const out = { view, rings: [], spokes: [], sectorLabels: [], cnodes: [], memNodes: [], cedges: [], sedges: [], stems: [], plate: null, regions: [], loopNodes: [], planNodes: [], pos: {}, tokens: 0, lit: 0, hub: { x: cxp, y: cyp, hid: view === 'flow' || view === 'time' } };
    // the plot's pan/zoom, for what is drawn OUTSIDE it (the lanes) but joins a record inside it
    const GZ = (S.pan && S.pan.z) || 1, GX = (S.pan && S.pan.x) || 0, GY = (S.pan && S.pan.y) || 0;
    const atP = (p) => ({ x: PW / 2 + (p.x - PW / 2) * GZ + GX, y: PH / 2 + (p.y - PH / 2) * GZ + GY });
    const edge = (list, a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; list.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls, title }); };
    const bucket = {};
    const place = (si, i, n, score, id) => {
      const mid = -90 + si * step, half = step / 2 - 5;
      const r = Math.min(RMAX, 52 + (1 - score) * (RMAX - 52) / 0.4);
      const a = n === 1 ? mid : mid - half + (i + 0.5) * ((half * 2) / n);
      const gx = cxp + Math.cos(a * RAD) * r, gy = cyp + Math.sin(a * RAD) * r;
      if (view === 'galaxy') return { x: gx, y: gy };
      if (view === 'iso') { const z = score * 70; const p = isoP(gx, gy, z); out.stems.push({ x: px(p.x), y: px(p.y), h: px(z) }); return p; }
      if (view === 'flow') { const cw = (PW - LANE_L - 24) / Math.max(srcs.length, 1); return { x: LANE_L + 12 + cw * (si + 0.5), y: LANE_T + 44 + Math.min(1, (1 - score) / 0.4) * (PH - LANE_T - 150) }; }
      const cols = Math.max(turnKeys.length, 1) + 1, k = firstRead(id), cw = (PW - LANE_L - 60) / cols, lh = (PH - LANE_T - 120) / Math.max(srcs.length, 1);
      const key = k + ':' + si, nth = (bucket[key] = (bucket[key] || 0) + 1) - 1;
      return { x: LANE_L + 30 + cw * (k ? k - 0.5 : cols - 0.5) + (nth % 2) * 10, y: LANE_T + 50 + lh * si + nth * 19 };
    };
    if (view === 'galaxy') [0.90, 0.75, 0.60].forEach((sc) => { const r = Math.min(RMAX, 52 + (1 - sc) * (RMAX - 52) / 0.4); out.rings.push({ cx: px(cxp), cy: px(cyp), d: px(r * 2) }); });
    if (view === 'time') { const cols = Math.max(turnKeys.length, 1) + 1, cw = (PW - LANE_L - 60) / cols; for (let k = 1; k <= cols; k++) out.sectorLabels.push({ name: k < cols ? (turnKeys[k - 1] || 'm' + k) : 'never', col: 'var(--cg-t3)', x: px(LANE_L + 30 + cw * (k - 0.5)), y: px(LANE_T + 12) }); }
    if (view === 'iso') { const s = RMAX + 40; const c = isoP(cxp, cyp, 0); out.plate = { x: px(c.x - s * 0.78 * 1.05), y: px(c.y - s * 0.39 * 2 * 1.05), w: px(s * 0.78 * 2.1), h: px(s * 0.39 * 4.2) }; }
    srcs.forEach((s, si) => {
      const list = ctx.filter((n) => (n.source || '?') === s).sort((a, b) => (b.score || 0) - (a.score || 0));
      const mid = -90 + si * step, col = color(s);
      if (view === 'galaxy') { out.spokes.push({ x: px(cxp), y: px(cyp), len: px(RMAX + 10), deg: (mid - step / 2) }); out.sectorLabels.push({ name: s, col, x: px(cxp + Math.cos(mid * RAD) * (RMAX + 26)), y: px(cyp + Math.sin(mid * RAD) * (RMAX + 26)) }); }
      else if (view === 'iso') { const p = isoP(cxp + Math.cos(mid * RAD) * (RMAX + 26), cyp + Math.sin(mid * RAD) * (RMAX + 26), 0); out.sectorLabels.push({ name: s, col, x: px(p.x), y: px(p.y) }); }
      else if (view === 'flow') { const cw = (PW - LANE_L - 24) / srcs.length; out.sectorLabels.push({ name: s, col, x: px(LANE_L + 12 + cw * (si + 0.5)), y: px(LANE_T + 26) }); }
      else { const lh = (PH - LANE_T - 120) / srcs.length; out.sectorLabels.push({ name: s, col, x: px(LANE_L + 8), y: px(LANE_T + 50 + lh * si), lane: true }); }
      list.forEach((n, i) => {
        const score = Math.max(0, Math.min(1, n.score == null ? 0.5 : +n.score)), tok = tokOf(n);
        const q = place(si, i, list.length, score, n.id);
        const inFocus = focus.has(n.id) && n.included !== false, by = readBy(n.id), everRead = by.length > 0, ghost = n.included === false;
        if (inFocus) { out.tokens += tok; out.lit++; }
        const d = Math.max(17, 7 + Math.sqrt(tok) / 2.7 + 8);
        out.pos[n.id] = { x: q.x, y: q.y, col, source: s, label: n.label || n.id, lit: inFocus, rim: d / 2, ghost, score, tok, kind: n.type || s };
        out.cnodes.push({ id: n.id, x: px(q.x), y: px(q.y), d: px(d), col, cls: (inFocus ? 'lit ' : everRead ? '' : 'dim ') + (ghost ? 'ghost ' : '') + (n.type === 'dataset' ? 'sq ' : '') + (S.sel === n.id ? 'on' : ''),
          op: (inFocus ? 1 : everRead ? 0.55 + score * 0.3 : ghost ? 0.4 : 0.55).toFixed(2),
          title: (n.label || n.id) + ' · ' + s + (n.type ? ' · ' + n.type : '') + ' · relevance ' + score.toFixed(2) + ' · ' + tok + ' tokens' + (ghost ? ' · related, not injected' : inFocus ? ' · in this prompt' : everRead ? ' · read by ' + by.join(', ') : '') });
      });
    });
    if (ctx.length && (view === 'galaxy' || view === 'iso')) out.regions.push({ x: px(cxp - 26), y: px(view === 'iso' ? PH - 26 : Math.min(PH - 16, cyp + RMAX + 40)), col: 'var(--cg-t3)', t: 'context' });
    // memory on the outer arc, hollow where it was never injected
    if (mem.length) {
      const R1 = RMAX + 44, R2 = RMAX + 74, ROW = 26, PER = 22;      // the arcs: injected inside, the rest in rows outside
      const memAt = (a, R, row) => { if (view === 'galaxy') return { x: cxp + Math.cos(a * RAD) * R, y: cyp + Math.sin(a * RAD) * R }; if (view === 'iso') return isoP(cxp + Math.cos(a * RAD) * R, cyp + Math.sin(a * RAD) * R, 0); return { x: LANE_L + 40 + ((a + 150) / 300) * (PW - LANE_L - 80), y: PH - 50 + row * 20 }; };
      const byTime = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''));
      const inj = mem.filter((n) => n._injected).sort(byTime), gh = ghosts ? mem.filter((n) => !n._injected).sort(byTime) : [];
      const memCol = (n) => (n._sess && S.memColor && S.memColor(n.type)) || color('memory');
      const shape = (n) => n.type === 'message' ? 'msg' : n.type === 'session' ? 'sess' : /^dag/.test(n.type || '') ? 'dag' : '';
      const arc = (list, R0, ghost) => list.forEach((n, i) => { const row = Math.floor(i / PER), k = i % PER, nrow = Math.min(PER, list.length - row * PER);
        const a = nrow === 1 ? 0 : -150 + k * (300 / (nrow - 1)); const q = memAt(a, R0 + row * ROW, row + (ghost ? 1 : 0)); const lit = !ghost && focus.has(n.id);
        out.pos[n.id] = { x: q.x, y: q.y, col: memCol(n), source: 'memory', label: n.label || n.id, lit, rim: 6, ghost, score: +n.score || 0, tok: tokOf(n), kind: n.type || 'memory', rec: n.rec || n, sess: !!n._sess }; if (lit) { out.tokens += tokOf(n); out.lit++; }
        out.memNodes.push({ id: n.id, x: px(q.x), y: px(q.y), col: memCol(n), cls: shape(n) + ' ' + (ghost ? 'ghost ' : lit ? 'lit ' : '') + (S.sel === n.id ? 'on' : ''), title: (n.label || n.id) + ' · ' + (n.type || 'memory') + (ghost ? ' · in the session, not injected' : lit ? ' · in this prompt' : ' · injected') }); });
      arc(inj, R1, false); arc(gh, R2, true);
      out.regions.push(view === 'galaxy' ? { x: px(Math.min(PW - 60, cxp + R1 - 10)), y: px(Math.min(PH - 14, cyp + R1 - 4)), col: 'var(--cg-ac2)', t: 'memory' + (memSess.length ? ' · session ' + memSess.length : '') } : { x: px(PW - 70), y: px(PH - 74), col: 'var(--cg-ac2)', t: 'memory' });
      // the session graph's own relations (FOLLOWS · RESPONDS · CAUSES · DERIVED …) among what is drawn, and into the prompt's records
      const hide = S.memHide || new Set();
      (S.memEdges || []).forEach((e) => { const rel = String(e.relation || e.type || ''); if (hide.has(rel)) return; const a = out.pos[e.from_id || e.from], b = out.pos[e.to_id || e.to]; if (!a || !b) return;
        const col = (S.edgeColor && S.edgeColor(rel)) || 'var(--cg-ac2)'; edge(out.cedges, a, b, col, 'mem' + (a.lit && b.lit ? ' lit' : ''), a.label + ' → ' + b.label + ' · ' + rel.replace(/_/g, ' ').toLowerCase()); });
    }
    const lpos = [];
    if (loop.length) {
      loop.forEach((s, i) => { const y = LANE_T + 22 + i * 44; lpos.push({ x: LANE_L - 12, y, st: s.status });
        out.loopNodes.push({ i, x: px(10), y: px(y), label: (i + 1) + ' ' + (s.label || s.id), cap: s.cap || '', ms: s.ms || '', cls: (s.status === 'ok' ? 'done' : s.status === 'running' ? 'run' : s.status === 'fail' ? 'fail' : 'pend') + (S.lsel === i ? ' sel' : ''), title: 'loop step ' + (i + 1) + ' · ' + (s.status || 'pending') + (s.cap ? ' · ' + s.cap : '') + ' · click to see what it read' }); });
      const done = loop.filter((s) => s.status === 'ok').length;
      out.regions.push({ x: px(10), y: px(LANE_T - 8 < 2 ? 2 : LANE_T - 8), col: 'var(--cg-ac)', t: 'loop · ' + done + ' of ' + loop.length });
    }
    if (plan.length) {
      const pw = (PW - LANE_L - 40) / plan.length;
      plan.forEach((p, i) => { const x = LANE_L + 20 + pw * (i + 0.5), st = p.status === 'done' || p.status === 'complete' || p.status === 'completed' ? 'done' : p.status === 'running' || p.status === 'active' || p.status === 'in_progress' ? 'run' : 'pend';
        out.planNodes.push({ x: px(x), y: px(18), lx: px(x), ly: px(i % 2 ? 44 : 32), label: p.label || p.id, cls: st, title: 'plan step ' + (i + 1) + ' · ' + (p.label || p.id) + ' · ' + (p.status || '') });
        if (st === 'run' && loop.length) { const ri = loop.findIndex((s) => s.status === 'running'); const b = lpos[ri >= 0 ? ri : loop.length - 1]; if (b) edge(out.sedges, { x, y: 26 }, b, 'var(--cg-ac)', 'mem', 'plan step ' + (i + 1) + ' → the loop step running it'); } });
      out.regions.push({ x: px(LANE_L + 20), y: px(1), col: 'var(--cg-t2)', t: 'plan · ' + plan.length + ' steps · ' + plan.filter((p) => /done|complete/.test(p.status || '')).length + ' done' });
    }
    // relations INSIDE the graph: what cites what (the context's own edges)
    (S.edges || []).forEach((e) => { const a = out.pos[e.from], b = out.pos[e.to]; if (!a || !b) return; const lit = a.lit && b.lit; const memRel = a.source === 'memory' || b.source === 'memory';
      edge(out.cedges, a, b, memRel ? 'var(--cg-ac2)' : lit ? a.col : 'var(--cg-bd2)', memRel ? 'mem' : lit ? 'rel lit' : 'rel', a.label + ' → ' + b.label + (e.label ? ' · ' + String(e.label).replace(/_/g, ' ').toLowerCase() : '')); });
    // the step in focus (or the one running), wired to the records it read — from the fixed lane into the moving plot
    stepReads.forEach((ids, si) => { const a = lpos[si]; if (!a) return; const lit = S.lsel != null ? S.lsel === si : a.st === 'running'; if (!lit && S.lsel != null) return;
      (ids || []).forEach((id) => { const b = out.pos[id]; if (!b) return; edge(out.sedges, a, atP(b), lit ? 'var(--cg-ac)' : 'var(--cg-bd2)', lit ? 'used' : 'rel', 'step ' + (si + 1) + ' read ' + b.label); }); });
    // the record open in the panel
    out.rec = null;
    if (S.sel && out.pos[S.sel]) { const r = out.pos[S.sel]; const by = readBy(S.sel), steps = readBySteps(S.sel);
      const rels = (S.edges || []).filter((e) => e.from === S.sel || e.to === S.sel).map((e) => { const o = out.pos[e.from === S.sel ? e.to : e.from]; return (o ? o.label : '?') + (e.label ? ' — ' + String(e.label).replace(/_/g, ' ').toLowerCase() : ''); }).slice(0, 4);
      out.rec = { id: S.sel, name: r.label, kind: r.source + ' · ' + r.kind, col: r.col, turn: by[0] || null, ghost: r.ghost, family: r.source === 'memory' ? 'memory' : 'context', rec: r.rec || null,
        rows: r.sess ? [{ k: 'kind', v: r.kind + (r.rec && r.rec.source_type ? ' · ' + r.rec.source_type : '') }, { k: 'recalled', v: r.ghost ? 'in the session, never injected' : 'injected' + (by.length ? ' · ' + by.join(', ') : '') }, { k: 'created', v: String((r.rec && r.rec.created_at) || '').replace('T', ' ').slice(0, 16) || '—' }, { k: 'importance', v: r.score.toFixed(2) }]
          : [{ k: 'relevance', v: r.score.toFixed(2) + (r.ghost ? ' · related, not injected' : r.lit ? ' · in this prompt' : by.length ? ' · in the prompt of ' + by.join(', ') : ' · not read') }, { k: 'tokens', v: String(r.tok) }, { k: 'read by', v: by.length ? by.join(' · ') : '—' }, { k: 'loop steps', v: steps.length ? steps.map((s) => 'step ' + s).join(' · ') : '—' }, { k: 'source', v: r.source + ' · ' + r.kind }], rels }; }
    // All edges off: only the relations that touch the prompt (lit, used, memory) stay; the dim ones fold away
    if (!S.allEdges) { out.cedges = out.cedges.filter((e) => e.cls !== 'rel'); out.sedges = out.sedges.filter((e) => e.cls !== 'rel'); }
    out.srcs = srcs.map((s) => ({ name: s, col: color(s), n: ctx.filter((n) => (n.source || '?') === s).length }));
    out.offSrcs = [...new Set(nodes.filter((n) => n.source !== 'memory').map((n) => n.source || '?'))].filter((s) => off.has(s)).map((s) => ({ name: s, col: color(s), n: nodes.filter((n) => (n.source || '?') === s).length }));
    // the other graphs' chips: the same row, the same toggle
    out.families = [];
    const nMem = memCtx.length + (S.memory || []).filter((m) => m && m.id && !ctxIds.has(m.id)).length;
    if (nMem) out.families.push({ name: 'memory', col: color('memory'), n: nMem, on: !off.has('memory') });
    if ((S.dag || []).length && !(S.loop && S.loop.length)) out.families.push({ name: 'dag', col: 'var(--cg-ac)', n: S.dag.length, on: !off.has('loop') });
    if (S.loop && S.loop.length) out.families.push({ name: 'loop', col: 'var(--cg-ac)', n: S.loop.length, on: !off.has('loop') });
    if ((S.plan || []).length) out.families.push({ name: 'plan', col: 'var(--cg-t2)', n: S.plan.length, on: !off.has('plan') });
    out.ghosts = nodes.filter((n) => n.included === false).length + memSess.filter((n) => !n._injected).length;
    out.lanes = { l: LANE_L, t: LANE_T, hasLanes };
    return out;
  }

  /* ── the loop lane from the loop's events (the same stream <vera-loop-graph> reads), through families.js ── */
  function loopFromEvents(evs) {
    const F = root.VeraGraphFamilies; if (!F) return { steps: [], stepReads: [] };
    const d = F.toDoc('loop', { events: evs }); if (d.error) return { steps: [], stepReads: [] };
    const caps = {}; d.edges.forEach((e) => { if (e.label === 'CALLS') (caps[e.from] = caps[e.from] || []).push(e.to); });
    const byId = {}; d.nodes.forEach((n) => { byId[n.id] = n; });
    const steps = d.nodes.filter((n) => n.kind === 'step').map((n) => { const c = (caps[n.id] || []).map((id) => byId[id] && byId[id].label).filter(Boolean); return { id: n.id, label: n.label, status: n.status, cap: c.join(' · '), ms: n.rec && n.rec.ms != null ? (n.rec.ms >= 1000 ? (n.rec.ms / 1000).toFixed(1) + ' s' : n.rec.ms + ' ms') : '' }; });
    // what each step read: the wires the cap events carried, when they name context ids
    const stepReads = steps.map((s) => (caps[s.id] || []).flatMap((id) => (byId[id] && byId[id].wires) || []).filter((w) => typeof w === 'string'));
    return { steps, stepReads };
  }
  function planFromGoals(goals) {
    const F = root.VeraGraphFamilies; const list = Array.isArray(goals) ? goals : (goals && goals.goals) || [];
    if (F) { const d = F.toDoc('plan', { goals: list }); if (!d.error) return d.nodes.filter((n) => n.parents.length === 0).slice(0, 8).map((n) => ({ id: n.id, label: n.label, status: n.status })); }
    return list.slice(0, 8).map((g) => ({ id: 'goal:' + (g.id || g.slug || g.name), label: g.name || g.title || g.slug, status: g.status || '' }));
  }

  const CSS = `
vera-context-graph{display:flex;flex-direction:column;min-height:0;min-width:0;position:relative;--cg-bg:var(--bg0,#0e0f12);--cg-s1:var(--bg1,#15171c);--cg-s2:var(--bg2,#1b1e25);--cg-bd:var(--border,#2a2e37);--cg-bd2:color-mix(in srgb,var(--border,#2a2e37) 70%,var(--fg,#ddd));--cg-t1:var(--fg,#e6e6e6);--cg-t2:var(--dim,#aaa);--cg-t3:var(--dim2,#777);--cg-ac:var(--acc,#7c9cff);--cg-ac2:var(--acc2,#5ec9a0);--cg-mono:var(--mono,ui-monospace,monospace);font-size:10.5px;color:var(--cg-t1)}
vera-context-graph .cg-hd{flex-shrink:0;display:flex;align-items:center;gap:8px;padding:8px 10px 0;flex-wrap:wrap}
vera-context-graph .cg-hd h2{font-size:12px;font-weight:600;margin:0}
vera-context-graph .cg-hd .lbl{font-family:var(--cg-mono);font-size:9px;color:var(--cg-t3)}
vera-context-graph .cg-hd .sp{flex:1}
vera-context-graph .cg-seg{display:inline-flex;gap:2px;background:var(--cg-s2);border-radius:6px;padding:2px}
vera-context-graph .cg-seg button{font:inherit;font-size:9.5px;height:20px;padding:0 8px;border:none;border-radius:4px;background:transparent;color:var(--cg-t3);cursor:pointer}
vera-context-graph .cg-seg button.on{background:var(--cg-s1);color:var(--cg-t1);box-shadow:0 0 0 1px var(--cg-bd)}
vera-context-graph .cg-btn{font:inherit;font-size:9.5px;height:20px;padding:0 8px;border:1px solid var(--cg-bd);border-radius:5px;background:var(--cg-s2);color:var(--cg-t2);cursor:pointer}
vera-context-graph .cg-key{flex-shrink:0;display:flex;gap:12px;padding:6px 10px 0;flex-wrap:wrap}
vera-context-graph .cg-key span{font-size:9px;color:var(--cg-t3)}vera-context-graph .cg-key b{color:var(--cg-t2);font-weight:500}
vera-context-graph .cg-layers{flex-shrink:0;display:flex;align-items:center;gap:4px;padding:6px 10px 0;flex-wrap:wrap}
vera-context-graph .cg-lay{display:inline-flex;align-items:center;gap:5px;height:21px;padding:0 9px;border-radius:999px;border:none;background:var(--cg-s2);font:inherit;font-size:9.5px;color:var(--cg-t3);cursor:pointer}
vera-context-graph .cg-lay.on{color:var(--cg-t1);box-shadow:inset 0 0 0 1.5px currentColor}
vera-context-graph .cg-lay i{width:8px;height:8px;border-radius:50%;flex-shrink:0;opacity:.35}vera-context-graph .cg-lay.on i{opacity:1}
vera-context-graph .cg-lay i.s{border-radius:2px;background:transparent!important;box-shadow:inset 0 0 0 1.5px var(--cg-ac2)}
vera-context-graph .cg-lay b{font-family:var(--cg-mono);font-size:8.5px;font-weight:400;color:var(--cg-t3)}
vera-context-graph .cg-plot{flex:1;min-height:0;position:relative;margin:6px 10px 10px;overflow:hidden;cursor:grab;touch-action:none;user-select:none}
vera-context-graph .cg-plot.drag{cursor:grabbing}
vera-context-graph .cg-in{position:absolute;inset:0;transform-origin:50% 50%}
vera-context-graph .cg-ring{position:absolute;border-radius:50%;box-shadow:0 0 0 1px var(--cg-bd);transform:translate(-50%,-50%)}
vera-context-graph .cg-spoke{position:absolute;height:1px;background:var(--cg-bd);transform-origin:0 50%;opacity:.5}
vera-context-graph .cg-slbl{position:absolute;transform:translate(-50%,-50%);font-size:9.5px;letter-spacing:.05em;font-weight:600;white-space:nowrap;pointer-events:none}
vera-context-graph .cg-slbl.lane{transform:translate(0,-50%);text-align:left}
vera-context-graph .cg-edge{position:absolute;height:1.5px;transform-origin:0 50%;border-radius:1px;opacity:.7;pointer-events:none}
vera-context-graph .cg-edge.rel{height:1px;opacity:.5}vera-context-graph .cg-edge.rel:not(.lit){height:0;opacity:.45;border-top:1px dashed var(--cg-bd2);background:none!important}
vera-context-graph .cg-edge.lit{opacity:1;height:2px}vera-context-graph .cg-edge.used{height:2px;opacity:.9;border-radius:2px}
vera-context-graph .cg-edge.mem{height:0;opacity:.75;border-top:1.5px dashed var(--cg-ac2);background:none!important}
vera-context-graph .cg-node{position:absolute;transform:translate(-50%,-50%);border-radius:50%;cursor:pointer;background:var(--nc);transition:transform .12s;display:flex;align-items:center;justify-content:center}
vera-context-graph .cg-node:hover{transform:translate(-50%,-50%) scale(1.4)}
vera-context-graph .cg-node.sq{border-radius:3px}
vera-context-graph .cg-node.dim{filter:saturate(.4)}
vera-context-graph .cg-node.ghost{background:transparent;box-shadow:inset 0 0 0 1.5px var(--nc)}
vera-context-graph .cg-node.lit{box-shadow:0 0 0 2px var(--cg-bg),0 0 0 3.5px var(--nc),0 0 14px -2px var(--nc)}
vera-context-graph .cg-node.on{box-shadow:0 0 0 3px var(--cg-bg),0 0 0 5px var(--cg-ac)}
vera-context-graph .cg-node span{font-family:var(--cg-mono);font-size:7px;color:var(--cg-bg);opacity:.9;pointer-events:none;max-width:90%;overflow:hidden;white-space:nowrap}
vera-context-graph .cg-mem{position:absolute;transform:translate(-50%,-50%);width:12px;height:12px;border-radius:3px;cursor:pointer;--mc:var(--cg-ac2);background:var(--mc)}
vera-context-graph .cg-mem.msg{width:16px;height:9px;border-radius:3px}vera-context-graph .cg-mem.sess{transform:translate(-50%,-50%) rotate(45deg);border-radius:1px}vera-context-graph .cg-mem.dag{border-radius:50%;border:1.5px dashed var(--mc);box-sizing:border-box}
vera-context-graph .cg-mem.ghost{background:transparent!important;box-shadow:inset 0 0 0 1.5px var(--mc);opacity:.55}
vera-context-graph .cg-lay.fam{margin-left:2px;box-shadow:inset 0 0 0 1px var(--cg-bd)}vera-context-graph .cg-lay i.p{border-radius:999px;width:13px;height:6px}vera-context-graph .cg-lay i.d{border-radius:1px;transform:rotate(45deg)}
vera-context-graph .cg-btn.on{color:var(--cg-t1);border-color:var(--cg-ac)}
vera-context-graph .cg-mem.lit,vera-context-graph .cg-mem.on{box-shadow:0 0 0 2px var(--cg-bg),0 0 0 4px var(--cg-t1)}
vera-context-graph .cg-plate{position:absolute;clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%);background:linear-gradient(180deg,color-mix(in srgb,var(--cg-ac) 9%,transparent),color-mix(in srgb,var(--cg-ac) 3%,transparent));pointer-events:none}
vera-context-graph .cg-stem{position:absolute;width:1px;background:color-mix(in srgb,var(--cg-t3) 60%,transparent);transform:translate(-50%,0);pointer-events:none}
vera-context-graph .cg-region{position:absolute;font-size:8.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:600;pointer-events:none}
vera-context-graph .cg-hub{position:absolute;transform:translate(-50%,-50%);width:52px;height:52px;border-radius:50%;background:var(--cg-s1);box-shadow:0 0 0 1.5px var(--cg-ac);display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
vera-context-graph .cg-hub b{font-size:11px;font-weight:600;color:var(--cg-ac)}vera-context-graph .cg-hub span{font-family:var(--cg-mono);font-size:8px;color:var(--cg-t2)}
vera-context-graph .cg-hub.hid{display:none}
vera-context-graph .cg-loop{position:absolute;transform:translate(0,-50%);height:36px;width:100px;padding:0 8px;border-radius:6px;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:1px;line-height:1.15;font-family:var(--cg-mono);font-size:8.5px;background:var(--cg-s2);white-space:nowrap;cursor:pointer;box-sizing:border-box}
vera-context-graph .cg-loop b{font-weight:600;font-size:9px}vera-context-graph .cg-loop .c{font-size:7.5px;color:var(--cg-t3);max-width:84px;overflow:hidden;text-overflow:ellipsis}vera-context-graph .cg-loop .t{position:absolute;right:6px;top:4px;font-size:7.5px;color:var(--cg-t3)}
vera-context-graph .cg-loop.done{color:var(--cg-ac2);box-shadow:inset 0 0 0 1px var(--cg-ac2)}vera-context-graph .cg-loop.run{color:var(--cg-ac);box-shadow:inset 0 0 0 1.5px var(--cg-ac);background:color-mix(in srgb,var(--cg-ac) 15%,transparent)}
vera-context-graph .cg-loop.pend{color:var(--cg-t3);box-shadow:inset 0 0 0 1px var(--cg-bd2)}vera-context-graph .cg-loop.fail{color:#e06c75;box-shadow:inset 0 0 0 1px #e06c75}
vera-context-graph .cg-loop.sel{box-shadow:inset 0 0 0 1.5px var(--cg-ac),0 0 0 2px var(--cg-bg),0 0 0 3.5px color-mix(in srgb,var(--cg-ac) 45%,transparent)}
vera-context-graph .cg-plan{position:absolute;transform:translate(-50%,-50%) rotate(45deg);width:10px;height:10px;border-radius:1px}
vera-context-graph .cg-plan.done{background:var(--cg-ac2)}vera-context-graph .cg-plan.run{background:var(--cg-ac);box-shadow:0 0 0 2px var(--cg-bg),0 0 0 3.5px color-mix(in srgb,var(--cg-ac) 45%,transparent)}vera-context-graph .cg-plan.pend{background:var(--cg-t3)}
vera-context-graph .cg-planl{position:absolute;transform:translate(-50%,0);font-family:var(--cg-mono);font-size:7.5px;color:var(--cg-t3);white-space:nowrap;max-width:92px;overflow:hidden;text-overflow:ellipsis;text-align:center}
vera-context-graph .cg-planl.done{color:var(--cg-ac2)}vera-context-graph .cg-planl.run{color:var(--cg-ac)}
vera-context-graph .cg-rec{position:absolute;left:10px;right:10px;bottom:10px;z-index:8;display:flex;flex-direction:column;gap:3px;padding:10px 12px;border-radius:8px;background:color-mix(in srgb,var(--cg-s2) 97%,var(--gc));box-shadow:0 1px 2px rgba(0,0,0,.2),0 10px 30px -16px rgba(0,0,0,.6),0 0 0 1px var(--cg-bd);font-size:10.5px;cursor:default}
vera-context-graph .cg-rec-h{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:600;min-width:0;margin-bottom:3px}
vera-context-graph .cg-rec-h i{width:9px;height:9px;border-radius:50%;background:var(--gc);flex:none}vera-context-graph .cg-rec-h i.ghost{background:transparent;box-shadow:inset 0 0 0 1.5px var(--gc)}
vera-context-graph .cg-rec-h b{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
vera-context-graph .cg-rec-h .mono{font-family:var(--cg-mono);font-size:9px;color:var(--cg-t3);font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
vera-context-graph .cg-rec-h .x{margin-left:auto;flex:none;width:18px;height:18px;border-radius:4px;display:flex;align-items:center;justify-content:center;color:var(--cg-t3);cursor:pointer}vera-context-graph .cg-rec-h .x:hover{color:var(--cg-t1);background:var(--cg-s1)}
vera-context-graph .cg-rec-r{display:flex;gap:8px;align-items:baseline;font-family:var(--cg-mono);font-size:9.5px}vera-context-graph .cg-rec-r .k{width:66px;flex:none;color:var(--cg-t3)}vera-context-graph .cg-rec-r .v{color:var(--cg-t1);min-width:0}
vera-context-graph .cg-rec-l{display:flex;gap:6px;align-items:center;font-family:var(--cg-mono);font-size:9px;color:var(--cg-t2)}vera-context-graph .cg-rec-l i{width:12px;height:1.5px;background:var(--gc);flex:none}
vera-context-graph .cg-rec-a{display:flex;gap:6px;margin-top:5px}
vera-context-graph .cg-rec-a button{font:inherit;font-size:10px;color:var(--cg-t2);background:var(--cg-s1);border:none;border-radius:4px;padding:4px 8px;cursor:pointer;box-shadow:0 0 0 1px var(--cg-bd)}
vera-context-graph .cg-rec-a button.pri{color:var(--cg-bg);background:var(--cg-ac);box-shadow:none}
vera-context-graph .cg-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--cg-t3);font-size:10.5px;line-height:1.5;padding:20px;pointer-events:none}
`;
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-context-graph-css')) return; const s = doc.createElement('style'); s.id = 'vera-context-graph-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  /* ── the element ─────────────────────────────────────────────────────────────────────────────────── */
  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-context-graph')) {
    class VeraContextGraph extends HTMLElement {
      constructor() { super(); this._S = { view: 'galaxy', nodes: [], edges: [], focus: [], reads: {}, stepReads: [], loop: [], plan: [], dag: [], memory: [], memEdges: [], memHide: null, memColor: null, edgeColor: null, allEdges: false, layersOff: new Set(), related: true, sel: null, lsel: null, pan: { x: 0, y: 0, z: 1 }, color: null, turn: '' }; this._evs = []; this._raf = 0; this._drag = null; }
      connectedCallback() {
        ensureCss(this.ownerDocument); if (this._built) { this._schedule(); return; } this._built = true;
        const a = this.getAttribute('view'); if (a) this._S.view = a;
        this.innerHTML = '<div class="cg-hd"><h2>Context graph</h2><span class="lbl" data-r="tok"></span><span class="sp"></span><span class="cg-seg" data-r="views" title="The unified graph\'s layouts, here"></span><button class="cg-btn" data-a="alledges" data-r="alledges" title="Draw every relation, not only the ones that touch the prompt">All edges</button><span class="lbl" data-r="zoom">100%</span><button class="cg-btn" data-a="fit" title="Back to the whole graph">Fit</button><button class="cg-btn" data-a="collapse" title="Fold the graph back into the quick menu">Collapse</button></div>'
          + '<div class="cg-key"><span><b>angle</b> = source</span><span><b>distance</b> = lower relevance</span><span><b>area</b> = tokens</span><span><b>hollow</b> = related, not injected</span></div>'
          + '<div class="cg-layers" data-r="layers"></div><div class="cg-plot" data-r="plot"><div class="cg-in" data-r="in"></div><div data-r="lanes"></div></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this._r.views.innerHTML = VIEWS.map((v) => '<button data-v="' + v[0] + '" title="' + esc(v[2]) + '">' + v[1] + '</button>').join('');
        this.addEventListener('click', (e) => this._click(e));
        const plot = this._r.plot;
        plot.addEventListener('wheel', (e) => { e.preventDefault(); const r = plot.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const p = this._S.pan; const nz = Math.max(0.5, Math.min(4, p.z * (e.deltaY > 0 ? 0.88 : 1.14))), k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._schedule(); }, { passive: false });
        plot.addEventListener('pointerdown', (e) => { if (e.button || (e.target.closest && e.target.closest('.cg-rec,.cg-node,.cg-mem,.cg-loop,button'))) return; e.preventDefault(); this._drag = { x0: e.clientX, y0: e.clientY, px: this._S.pan.x, py: this._S.pan.y, id: e.pointerId, moved: false }; });
        plot.addEventListener('pointermove', (e) => { const g = this._drag; if (!g) return; const dx = e.clientX - g.x0, dy = e.clientY - g.y0; if (!g.moved && Math.abs(dx) + Math.abs(dy) > 4) { g.moved = true; plot.classList.add('drag'); try { plot.setPointerCapture(g.id); } catch (_) {} } if (g.moved) { this._S.pan.x = g.px + dx; this._S.pan.y = g.py + dy; this._schedule(); } });
        const up = () => { if (this._drag) { plot.classList.remove('drag'); this._drag = null; } }; plot.addEventListener('pointerup', up); plot.addEventListener('pointercancel', up);
        if (root.ResizeObserver) { this._ro = new ResizeObserver(() => this._schedule()); this._ro.observe(plot); }
        this._schedule();
      }
      disconnectedCallback() { if (this._ro) { try { this._ro.disconnect(); } catch (_) {} } }
      // ── API ──
      setContext(nodes, edges, o) { o = o || {}; const S = this._S; S.nodes = Array.isArray(nodes) ? nodes : []; S.edges = Array.isArray(edges) ? edges : []; if (o.focus) S.focus = o.focus; if (o.reads) S.reads = o.reads; if (o.stepReads) S.stepReads = o.stepReads; if (o.color) S.color = o.color; if (o.turn != null) S.turn = o.turn; if (S.sel && !S.nodes.some((n) => n.id === S.sel)) S.sel = null; this._schedule(); }
      setLoopEvents(evs) { this._evs = Array.isArray(evs) ? evs.slice() : []; this._loopRefresh(); }
      appendLoopEvent(ev) { if (!ev || typeof ev !== 'object') return; if (ev.type === 'start' || /\.triage_start$/.test(String(ev.type || ''))) this._evs = []; this._evs.push(ev); if (this._evs.length > 4000) this._evs = this._evs.slice(-3000); this._loopRefresh(); }
      _loopRefresh() { const L = loopFromEvents(this._evs); this._S.loop = L.steps; if (!this._S.stepReadsPinned) this._S.stepReads = L.stepReads; this._schedule(); }
      setPlan(goals) { this._S.plan = planFromGoals(goals); this._schedule(); }
      // the rail's Memory graph: the session's records and their relations (the same data, drawn here on the arc)
      setMemory(nodes, edges, o) { o = o || {}; const S = this._S; S.memory = Array.isArray(nodes) ? nodes : []; S.memEdges = Array.isArray(edges) ? edges : []; if (o.color) S.memColor = o.color; if (o.edgeColor) S.edgeColor = o.edgeColor; if (o.hide) S.memHide = o.hide; this._schedule(); }
      // the rail's DAG graph: the run's planned cap chain, as the loop lane while no loop is live
      setDag(nodes) { this._S.dag = (Array.isArray(nodes) ? nodes : []).map((n, i) => ({ id: 'dag:' + (n.id != null ? n.id : i), label: String(n.cap || n.label || n.id), status: n.status === 'done' ? 'ok' : n.status === 'running' ? 'running' : n.status === 'err' || n.status === 'error' ? 'fail' : 'pending', cap: n.out || '', ms: '' })); this._schedule(); }
      allEdges(on) { if (on != null) { this._S.allEdges = !!on; this._schedule(); } return this._S.allEdges; }
      view(name) { if (name && VIEWS.some((v) => v[0] === name)) { this._S.view = name; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); } return this._S.view; }
      fit() { this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); }
      select(id) { this._S.sel = id || null; this._schedule(); }
      state() { return this._S; }
      // screen positions of the drawn records (for the runs to the message the host draws)
      positions() { const out = []; this.querySelectorAll('.cg-node,.cg-mem').forEach((el) => { const r = el.getBoundingClientRect(); const id = el.dataset.id; const p = this._last && this._last.pos[id]; if (!p) return; out.push({ id, x: r.left + r.width / 2, y: r.top + r.height / 2, rim: r.width / 2, col: p.col, source: p.source, lit: p.lit, ghost: p.ghost, label: p.label }); }); return out; }
      // ── render ──
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _click(e) {
        const t = e.target; const b = t.closest && t.closest('button[data-v]'); if (b) { this.view(b.dataset.v); return; }
        const a = t.closest && t.closest('[data-a]'); if (a) { const S = this._S; const k = a.dataset.a;
          if (k === 'fit') this.fit(); else if (k === 'collapse') { this.dispatchEvent(new CustomEvent('vera:ctx:collapse', { bubbles: true })); } else if (k === 'alledges') { S.allEdges = !S.allEdges; this._schedule(); } else if (k === 'layer') { const s = a.dataset.s; if (S.layersOff.has(s)) S.layersOff.delete(s); else S.layersOff.add(s); this._schedule(); }
          else if (k === 'related') { S.related = !S.related; this._schedule(); } else if (k === 'close') { S.sel = null; this._schedule(); }
          else if (k === 'toggle') { this.dispatchEvent(new CustomEvent('vera:ctx:toggle', { detail: { id: a.dataset.id }, bubbles: true })); }
          else if (k === 'open') { const r = this._last && this._last.rec; this.dispatchEvent(new CustomEvent('vera:ctx:pick', { detail: { id: a.dataset.id, open: true, family: r && r.id === a.dataset.id ? r.family : 'context', rec: r && r.id === a.dataset.id ? r.rec : null }, bubbles: true })); }
          else if (k === 'turn') { this.dispatchEvent(new CustomEvent('vera:ctx:focus-turn', { detail: { mid: a.dataset.mid }, bubbles: true })); }
          return; }
        const n = t.closest && t.closest('.cg-node,.cg-mem'); if (n) { const S = this._S; S.sel = S.sel === n.dataset.id ? null : n.dataset.id; S.lsel = null; this._schedule(); this.dispatchEvent(new CustomEvent('vera:ctx:pick', { detail: { id: n.dataset.id }, bubbles: true })); return; }
        const l = t.closest && t.closest('.cg-loop'); if (l) { const i = +l.dataset.i; const S = this._S; S.lsel = S.lsel === i ? null : i; S.sel = null; this._schedule(); }
      }
      _render() {
        const S = this._S, plot = this._r.plot; const W = plot.clientWidth || 600, H = plot.clientHeight || 500;
        const iso = root.VeraISO && typeof root.VeraISO.proj === 'function' ? (() => { const P = root.VeraISO.proj(30, 45, 1, true); const cx = (S.loop.length ? 118 : 0) + (W - (S.loop.length ? 118 : 0)) / 2, cy = (S.plan.length ? 52 : 0) + (H - (S.plan.length ? 52 : 0)) / 2; return (x, y, z) => { const p = P(x - cx, y - cy, z || 0); return { x: cx + p[0], y: cy + 40 + p[1] }; }; })() : null;
        const o = compute(Object.assign({}, S, { isoProj: iso }), W, H); this._last = o;
        const p = S.pan; this._r.in.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')';
        this._r.zoom.textContent = Math.round(p.z * 100) + '%';
        this._r.tok.textContent = (o.tokens ? o.tokens.toLocaleString() + ' tokens in the prompt' : '') + (S.turn ? (o.tokens ? ' · ' : '') + S.turn : '');
        this._r.views.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === o.view));
        if (this._r.alledges) this._r.alledges.classList.toggle('on', !!S.allEdges);
        const famChip = (f) => '<button class="cg-lay fam ' + (f.on ? 'on' : '') + '" data-a="layer" data-s="' + (f.name === 'dag' ? 'loop' : esc(f.name)) + '" style="color:' + esc(f.col) + '" title="' + esc(f.name) + ' · ' + f.n + ' · the ' + esc(f.name) + ' graph, folded into this one — click to fold it away"><i class="' + (f.name === 'plan' ? 'd' : f.name === 'memory' ? '' : 'p') + '" style="background:' + esc(f.col) + '"></i>' + esc(f.name) + '<b>' + f.n + '</b></button>';
        this._r.layers.innerHTML = o.srcs.concat(o.offSrcs).map((s) => '<button class="cg-lay ' + (S.layersOff.has(s.name) ? '' : 'on') + '" data-a="layer" data-s="' + esc(s.name) + '" style="color:' + esc(s.col) + '" title="' + esc(s.name) + ' · ' + s.n + ' records · click to fold this layer out of the graph"><i style="background:' + esc(s.col) + '"></i>' + esc(s.name) + '<b>' + s.n + '</b></button>').join('')
          + (o.families || []).map(famChip).join('')
          + (o.ghosts ? '<span style="flex:1"></span><button class="cg-lay ' + (S.related ? 'on' : '') + '" data-a="related" style="color:var(--cg-ac2)" title="Records related to this question that were not injected"><i class="s"></i>related<b>+' + o.ghosts + '</b></button>' : '');
        const st = (x, y) => 'left:' + x + 'px;top:' + y + 'px;';
        let h = '';
        o.rings.forEach((r) => { h += '<div class="cg-ring" style="' + st(r.cx, r.cy) + 'width:' + r.d + 'px;height:' + r.d + 'px"></div>'; });
        o.spokes.forEach((s) => { h += '<div class="cg-spoke" style="' + st(s.x, s.y) + 'width:' + s.len + 'px;transform:rotate(' + s.deg + 'deg)"></div>'; });
        if (o.plate) h += '<div class="cg-plate" style="' + st(o.plate.x, o.plate.y) + 'width:' + o.plate.w + 'px;height:' + o.plate.h + 'px"></div>';
        o.stems.forEach((s) => { h += '<div class="cg-stem" style="' + st(s.x, s.y) + 'height:' + s.h + 'px"></div>'; });
        o.sectorLabels.forEach((s) => { h += '<div class="cg-slbl' + (s.lane ? ' lane' : '') + '" style="' + st(s.x, s.y) + 'color:' + esc(s.col) + '">' + esc(s.name) + '</div>'; });
        o.cedges.forEach((e) => { h += '<div class="cg-edge ' + e.cls + '" title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;background:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.cnodes.forEach((n) => { h += '<div class="cg-node ' + n.cls + '" data-id="' + esc(n.id) + '" title="' + esc(n.title) + '" style="' + st(n.x, n.y) + 'width:' + n.d + 'px;height:' + n.d + 'px;--nc:' + esc(n.col) + ';opacity:' + n.op + '">' + (n.d >= 24 ? '<span>' + esc(String(n.id).replace(/^__\w+__/, '').slice(0, 6)) + '</span>' : '') + '</div>'; });
        o.memNodes.forEach((n) => { h += '<div class="cg-mem ' + n.cls + '" data-id="' + esc(n.id) + '" title="' + esc(n.title) + '" style="' + st(n.x, n.y) + (n.col ? ';--mc:' + esc(n.col) : '') + '"></div>'; });
        o.regions.forEach((r) => { h += '<div class="cg-region" style="' + st(r.x, r.y) + 'color:' + esc(r.col) + '">' + esc(r.t) + '</div>'; });
        h += '<div class="cg-hub' + (o.hub.hid ? ' hid' : '') + '" style="' + st(o.hub.x, o.hub.y) + '"><b>aide</b><span>' + (o.tokens >= 1000 ? (o.tokens / 1000).toFixed(1) + 'k' : o.tokens) + '</span></div>';
        this._r.in.innerHTML = h;
        // the lanes stay put while the plot pans and zooms
        let l = '';
        o.sedges.forEach((e) => { l += '<div class="cg-edge ' + e.cls + '" title="' + esc(e.title) + '" style="' + st(e.x, e.y) + 'width:' + e.len + 'px;background:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>'; });
        o.planNodes.forEach((n) => { l += '<div class="cg-plan ' + n.cls + '" title="' + esc(n.title) + '" style="' + st(n.x, n.y) + '"></div><div class="cg-planl ' + n.cls + '" style="' + st(n.lx, n.ly) + '">' + esc(n.label) + '</div>'; });
        o.loopNodes.forEach((n) => { l += '<div class="cg-loop ' + n.cls + '" data-i="' + n.i + '" title="' + esc(n.title) + '" style="' + st(n.x, n.y) + '"><b>' + esc(n.label.slice(0, 16)) + '</b><span class="c">' + esc(n.cap) + '</span><span class="t">' + esc(n.ms) + '</span></div>'; });
        if (o.rec) { const r = o.rec; l += '<div class="cg-rec" style="--gc:' + esc(r.col) + '"><span class="cg-rec-h"><i class="' + (r.ghost ? 'ghost' : '') + '"></i><b>' + esc(r.name) + '</b><span class="mono">' + esc(r.kind) + '</span><span class="x" data-a="close">✕</span></span>'
          + r.rows.map((x) => '<span class="cg-rec-r"><span class="k">' + esc(x.k) + '</span><span class="v">' + esc(x.v) + '</span></span>').join('') + r.rels.map((x) => '<span class="cg-rec-l"><i></i>' + esc(x) + '</span>').join('')
          + '<span class="cg-rec-a">' + (r.turn ? '<button class="pri" data-a="turn" data-mid="' + esc(r.turn) + '">Focus turn</button>' : '') + '<button data-a="toggle" data-id="' + esc(r.id) + '">' + (r.ghost ? 'Include in the prompt' : 'Exclude from the prompt') + '</button><button data-a="open" data-id="' + esc(r.id) + '">Open</button></span></div>'; }
        if (!o.cnodes.length && !o.memNodes.length) l += '<div class="cg-empty">' + (S.nodes.length ? 'Every layer is folded away — turn one back on above.' : 'The records the aide assembles for a turn appear here — send a message with context injection on.') + '</div>';
        this._r.lanes.innerHTML = l;
        this.dispatchEvent(new CustomEvent('vera:ctx:rendered', { detail: { view: o.view, tokens: o.tokens, lit: o.lit, nodes: o.cnodes.length + o.memNodes.length }, bubbles: true }));
      }
    }
    root.customElements.define('vera-context-graph', VeraContextGraph);
  }
  const api = { compute, loopFromEvents, planFromGoals, VIEWS, ensureCss, version: 1 };
  root.VeraContextGraph = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
