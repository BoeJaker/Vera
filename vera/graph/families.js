/* vera/graph/families.js — the ONE graph document's families (UI redesign, Notes/40 §7; the GraphViews board).
   ───────────────────────────────────────────────────────────────────────────────────────────────────────────
   Context, memory, the DAG run, the agentic loop, the plan and the estate are FAMILIES of one graph. Each family
   is an adapter that maps its own source to the same nodes and edges — with the fields the unified graph must keep
   (Note 39 §23): kind, layer, status, weight, time, real vs inferred, the non-authoritative flag, parallel groups,
   data wires, multiple parents. The mixer shows as much or as little of each family — off · focus · all — and the
   views (galaxy · iso · flow · timeline) draw one document whatever the mix.

   Pure functions, no DOM, no fetch — the memory graph panel feeds each adapter its payload (its own load, the
   chat's context, the loop's events posted in, topology.snapshot …) and draws the result. Served at
   /ui/graph/families.js; runs under node for the tests.

   API — window.VeraGraphFamilies = {
     FAMILIES                      [{id, label, icon, hue}] in sector order
     toDoc(family, payload)        → {family, nodes:[node], edges:[edge]}      one adapter's document
     merge(docs)                   → {nodes, edges}                            the documents as one graph (ids unique)
     counts(doc)                   → {family: n}
     mix(doc, settings, focusIds)  → the nodes/edges the mixer shows: off drops a family; focus keeps its nodes
                                     that touch a focused id (or the family's own edges to another family); all keeps it
     sector(family)                → [a0, a1] the family's angular band in the galaxy
   node = {id, family, kind, layer, label, status, weight (0..1), time (ms or 0), real (bool), nonAuthoritative,
           group, wires:[], parents:[], rec}            edge = {from, to, label, family, structural, real}          */
(function (root) {
  'use strict';
  const FAMILIES = [
    { id: 'turns',   label: 'Turns',   icon: '✎', hue: 205 },
    { id: 'context', label: 'Context', icon: '◎', hue: 165 },
    { id: 'memory',  label: 'Memory',  icon: '▤', hue: 265 },
    { id: 'dag',     label: 'DAG run', icon: '⋮', hue: 35 },
    { id: 'loop',    label: 'Loop',    icon: '↻', hue: 95 },
    { id: 'plan',    label: 'Plan',    icon: '☑', hue: 320 },
    { id: 'estate',  label: 'Estate',  icon: '⬡', hue: 190 },
  ];
  const IDS = FAMILIES.map((f) => f.id);
  const num = (v, d) => { const n = typeof v === 'number' ? v : parseFloat(v); return isFinite(n) ? n : (d == null ? 0 : d); };
  const ms = (t) => { if (!t) return 0; if (typeof t === 'number') return t < 1e12 ? t * 1000 : t; const n = Date.parse(t); return isFinite(n) ? n : 0; };
  const clamp01 = (v) => Math.max(0, Math.min(1, num(v, 0.5)));
  const node = (o) => ({
    id: String(o.id), family: o.family, kind: String(o.kind || 'node'), layer: String(o.layer || o.family),
    label: String(o.label == null ? o.id : o.label).slice(0, 80), status: String(o.status || ''), weight: clamp01(o.weight == null ? 0.5 : o.weight),
    time: ms(o.time), real: o.real !== false, nonAuthoritative: !!o.nonAuthoritative, group: o.group || '', wires: Array.isArray(o.wires) ? o.wires : [],
    parents: Array.isArray(o.parents) ? o.parents : [], rec: o.rec || {},
  });
  const edge = (from, to, label, family, extra) => Object.assign({ from: String(from), to: String(to), label: String(label || 'RELATED'), family, structural: true, real: true }, extra || {});

  /* ── the adapters ────────────────────────────────────────────────────── */
  const A = {};
  // MEMORY — /memory/graph/full: records with record_type · importance · created_at · source_type; edges {from_id,to_id,relation}
  A.memory = (p) => {
    const recs = (p && (p.nodes || p.records)) || [];
    const nodes = recs.filter((r) => r && r.id).map((r) => node({ id: r.id, family: 'memory', kind: r.record_type || '_unknown', layer: r.source_type || 'memory',
      label: r.summary || r.text || r.capability || r.id, status: r.status || '', weight: r.importance == null ? 0.5 : r.importance, time: r.created_at,
      real: !r._synthetic, nonAuthoritative: !!r.non_authoritative, group: r.run_id || '', parents: r.parent_run_id ? [r.parent_run_id] : [], rec: r }));
    const edges = ((p && p.edges) || []).filter((e) => e && (e.from_id || e.from) && (e.to_id || e.to)).map((e) => edge(e.from_id || e.from, e.to_id || e.to, e.relation || e.label, 'memory', { real: !e.inferred }));
    return { family: 'memory', nodes, edges };
  };
  // CONTEXT — the chat's assembled context (CTX_NODES / CTX_EDGES): {id, label, source, type, score, text, included}
  A.context = (p) => {
    const ns = (p && p.nodes) || [];
    const nodes = ns.filter((n) => n && n.id).map((n) => node({ id: n.id, family: 'context', kind: n.type || n.source || 'context', layer: n.source || n.src || 'context',
      label: n.label || n.title || n.id, status: n.included === false ? 'excluded' : 'included', weight: n.score == null ? 0.5 : n.score, time: n.ts || n.created_at || 0,
      real: true, rec: n }));
    const edges = ((p && p.edges) || []).filter((e) => e && (e.from || e.source) && (e.to || e.target)).map((e) => edge(e.from || e.source, e.to || e.target, e.label || e.rel || 'RELATED', 'context', { real: !e.inferred }));
    // every included node was retrieved into the turn it served: a structural edge to the turn when the payload names it
    if (p && p.turn) ns.forEach((n) => { if (n && n.id && n.included !== false) edges.push(edge(n.id, p.turn, 'RETRIEVED_INTO', 'context')); });
    return { family: 'context', nodes, edges };
  };
  // TURNS — the conversation's own messages: [{mid, role, text, ts}]
  A.turns = (p) => {
    const ts = (p && (p.turns || p.messages)) || [];
    const nodes = ts.filter((t) => t && (t.mid || t.id)).map((t, i) => node({ id: t.mid || t.id, family: 'turns', kind: t.role || 'turn', layer: 'turns', label: (t.text || t.summary || (t.role + ' ' + (i + 1))), time: t.ts, weight: 0.6, rec: t }));
    const edges = [];
    for (let i = 1; i < nodes.length; i++) edges.push(edge(nodes[i - 1].id, nodes[i].id, 'NEXT_IN_SESSION', 'turns'));
    return { family: 'turns', nodes, edges };
  };
  // DAG — the run shadow (/run/shadow/graph): non-authoritative projections of a run's steps
  A.dag = (p) => {
    const d = A.memory(p); d.family = 'dag';
    d.nodes.forEach((n) => { n.family = 'dag'; n.nonAuthoritative = true; n.layer = 'dag'; }); d.edges.forEach((e) => { e.family = 'dag'; });
    return d;
  };
  // LOOP — the agentic loop's SSE events (the same stream <vera-loop-graph> reads): start · step_start · step_done ·
  // cap / tool events · branch_open / merge / prune · done. Multiple parents and parallel groups come from the events.
  A.loop = (p) => {
    const evs = (p && (p.events || p)) || []; const nodes = new Map(); const edges = []; let runId = (p && p.run) || '';
    // a later event about the same node (step_done after step_start) updates only the fields it carries — the
    // parents, group and wires the first event set stay
    const put = (o) => { const n = node(o); if (nodes.has(n.id)) { const ex = nodes.get(n.id); Object.keys(o).forEach((k) => { if (k in n && k !== 'rec') ex[k] = n[k]; }); return ex; } nodes.set(n.id, n); return n; };
    const stepId = (ev) => 'step:' + (ev.step_id != null ? ev.step_id : (ev.step != null ? ev.step : (ev.index != null ? ev.index : '?')));
    (Array.isArray(evs) ? evs : []).forEach((ev) => {
      if (!ev || typeof ev !== 'object') return; const t = String(ev.type || ''); const time = ev.ts || ev.time || 0;
      if (t === 'start' || /\.start$/.test(t) || t === 'run_start') { runId = runId || ev.run_id || ev.stream_id || 'run'; put({ id: 'run:' + runId, family: 'loop', kind: 'run', label: ev.goal || ev.title || 'run', status: 'running', weight: 0.9, time }); return; }
      if (/step_start$/.test(t) || t === 'step') { const id = stepId(ev); put({ id, family: 'loop', kind: 'step', label: ev.title || ev.name || ev.goal || id, status: 'running', weight: 0.7, time, group: ev.branch || ev.group || '', parents: ev.parents || (ev.parent_step != null ? ['step:' + ev.parent_step] : []) });
        const par = (ev.parents && ev.parents[0]) || (ev.parent_step != null ? 'step:' + ev.parent_step : ('run:' + (runId || 'run'))); edges.push(edge(par, id, 'THEN', 'loop')); (ev.parents || []).slice(1).forEach((pp) => edges.push(edge(pp, id, 'THEN', 'loop'))); return; }
      if (/step_done$/.test(t) || /step_error$/.test(t) || /step_fail/.test(t)) { const id = stepId(ev); const o = { id, family: 'loop', kind: 'step', status: /done$/.test(t) && !ev.error ? 'ok' : 'fail', weight: 0.7, time }; if (ev.title || ev.name) o.label = ev.title || ev.name; const n = put(o); if (ev.ms != null) n.rec.ms = ev.ms; return; }
      if (/cap(_call|_start|\.start)$/.test(t) || t === 'tool_call' || t === 'cap' || t === 'cap.ok' || t === 'cap.fail' || /tool_(done|result)$/.test(t)) {
        const cap = ev.tool || ev.cap || ev.name || 'cap'; const sid = stepId(ev); const id = 'cap:' + sid + ':' + cap;
        put({ id, family: 'loop', kind: 'cap', label: cap, status: /ok|done|result/.test(t) ? 'ok' : (/fail/.test(t) ? 'fail' : 'running'), weight: 0.4, time, wires: ev.wires || [] });
        edges.push(edge(sid, id, 'CALLS', 'loop')); return; }
      if (/branch_open$/.test(t)) { const id = 'branch:' + (ev.branch || ev.id || '?'); put({ id, family: 'loop', kind: 'branch', label: ev.label || ev.branch || 'branch', status: 'running', weight: 0.5, time, group: ev.branch || ev.id || '' }); edges.push(edge(stepId(ev), id, 'FORKS', 'loop')); return; }
      if (/branch_merge$/.test(t) || /merge$/.test(t)) { const id = 'branch:' + (ev.branch || ev.id || '?'); put({ id, family: 'loop', kind: 'branch', label: ev.label || ev.branch || 'branch', status: 'ok', weight: 0.5, time }); edges.push(edge(id, stepId(ev), 'MERGES', 'loop')); return; }
      if (/branch_prune$/.test(t) || /prune$/.test(t)) { const id = 'branch:' + (ev.branch || ev.id || '?'); put({ id, family: 'loop', kind: 'branch', label: ev.label || ev.branch || 'branch', status: 'pruned', weight: 0.2, time }); return; }
      if (/(assess|verify|ledger|clarif|recover)/.test(t)) { const id = t.replace(/[^a-z0-9_]/gi, '_') + ':' + (ev.step_id != null ? ev.step_id : (ev.step != null ? ev.step : '')); put({ id, family: 'loop', kind: t.split('.').pop().split('_')[0], label: ev.title || ev.summary || t, status: ev.ok === false ? 'fail' : 'ok', weight: 0.35, time, real: false }); edges.push(edge(stepId(ev), id, 'RECORDS', 'loop', { structural: false, real: false })); return; }
      if (t === 'done' || /\.done$/.test(t) || t === 'run_done') { const n = nodes.get('run:' + (runId || 'run')); if (n) n.status = ev.ok === false ? 'fail' : 'ok'; }
    });
    return { family: 'loop', nodes: [...nodes.values()], edges };
  };
  // PLAN — goals: [{id, title, status, children:[…], parent}]
  A.plan = (p) => {
    const gs = (p && (p.goals || p.items || p)) || []; const nodes = []; const edges = [];
    const walk = (g, parent) => { if (!g || typeof g !== 'object') return; const id = 'goal:' + (g.id || g.title); nodes.push(node({ id, family: 'plan', kind: 'goal', label: g.title || g.name || g.id, status: g.status || g.state || '', weight: g.priority == null ? 0.5 : g.priority, time: g.updated_at || g.created_at, parents: parent ? [parent] : [], rec: g })); if (parent) edges.push(edge(parent, id, 'HAS_MILESTONE', 'plan')); (g.children || g.milestones || []).forEach((c) => walk(c, id)); };
    (Array.isArray(gs) ? gs : []).forEach((g) => walk(g, null));
    return { family: 'plan', nodes, edges };
  };
  // ESTATE — topology.snapshot: {nodes:[{id,name,kind,status}], edges|links:[{source,target,kind}]}
  A.estate = (p) => {
    const ns = (p && p.nodes) || []; const ls = (p && (p.edges || p.links)) || [];
    const nodes = ns.filter((n) => n && (n.id || n.name)).map((n) => node({ id: 'estate:' + (n.id || n.name), family: 'estate', kind: n.kind || n.type || n.category || 'node', layer: n.plane || n.category || 'estate', label: n.name || n.label || n.id, status: n.status || n.state || '', weight: n.load == null ? 0.5 : n.load, time: n.seen || n.ts || 0, rec: n }));
    const edges = ls.filter((e) => e && (e.source || e.from) && (e.target || e.to)).map((e) => edge('estate:' + (e.source || e.from), 'estate:' + (e.target || e.to), e.kind || e.label || 'LINK', 'estate'));
    return { family: 'estate', nodes, edges };
  };

  function toDoc(family, payload) {
    const f = String(family || '').toLowerCase();
    if (!A[f]) return { family: f, nodes: [], edges: [], error: 'unknown family ' + f };
    try { return A[f](payload); } catch (e) { return { family: f, nodes: [], edges: [], error: String(e && e.message || e) }; }
  }
  function merge(docs) {
    const nodes = new Map(); const edges = []; const seen = new Set();
    (docs || []).forEach((d) => { (d.nodes || []).forEach((n) => { if (!nodes.has(n.id)) nodes.set(n.id, n); }); (d.edges || []).forEach((e) => { const k = e.from + '→' + e.to + '·' + e.label; if (!seen.has(k)) { seen.add(k); edges.push(e); } }); });
    return { nodes: [...nodes.values()], edges };
  }
  function counts(doc) { const c = {}; IDS.forEach((f) => { c[f] = 0; }); (doc.nodes || []).forEach((n) => { c[n.family] = (c[n.family] || 0) + 1; }); return c; }
  // the mixer: settings = {family: 'off'|'focus'|'all'}; focusIds = the ids in focus (a selection, the turn's context)
  function mix(doc, settings, focusIds) {
    settings = settings || {}; const focus = new Set(focusIds || []);
    const mode = (f) => settings[f] || 'all';
    const byId = new Map((doc.nodes || []).map((n) => [n.id, n]));
    const touches = new Set();
    (doc.edges || []).forEach((e) => { const a = byId.get(e.from), b = byId.get(e.to); if (!a || !b) return; if (focus.has(e.from) || focus.has(e.to) || a.family !== b.family) { touches.add(e.from); touches.add(e.to); } });
    const keep = (n) => { const m = mode(n.family); if (m === 'off') return false; if (m === 'all') return true; return focus.has(n.id) || touches.has(n.id); };
    const nodes = (doc.nodes || []).filter(keep); const ids = new Set(nodes.map((n) => n.id));
    const edges = (doc.edges || []).filter((e) => ids.has(e.from) && ids.has(e.to));
    return { nodes, edges };
  }
  function sector(family) { const i = Math.max(0, IDS.indexOf(family)); const w = (Math.PI * 2) / IDS.length; return [-Math.PI / 2 + i * w, -Math.PI / 2 + (i + 1) * w]; }
  const api = { FAMILIES, toDoc, merge, counts, mix, sector, adapters: A, version: 1 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.VeraGraphFamilies = api;
})(typeof window !== 'undefined' ? window : null);
