/* The context graph — the chat's graph column (UI redesign: the Chat & canvas set's Graph board, "the LHM
   expanded: the Context menu grown into the full graph"; the GraphViews board, "the unified graph · views";
   Notes/40 §7 · §One page). The records the aide assembled for the turn in focus, drawn as a real graph, not a
   list: angle is the source, distance is lower relevance, area is tokens, hollow is related-but-not-injected.

   ONE graph of FAMILIES. The context is the plot's centre; the memory graph, the loop (or the run's DAG), the plan
   and the estate are families of the same graph — each drawn twice over, as the board does: as a LAYER inside the
   plot (the memory ring on its own arc, grouped by kind, in by relevance; the loop as a sector in the galaxy, a
   standing column of steps on the iso plate, a lane across the flow, a row on the timeline; the plan as its sector)
   AND as a linear TRACK around the edges (the loop down the left, the plan along the top, the estate strip along the
   bottom). The mixer sets each family off · in focus · everything; the source chips fold the context's layers.
   The relations cross the families: what cites what, which step read which record, which memory still relates,
   which plan step ran as which loop steps, which node a step ran on.

   The views: galaxy · iso (the same galaxy at a tilt chosen so the plate fills the column and never exceeds it; rings and sector discs
   lie on the floor, the hub and the loop column stand on pins) · flow · time · QUAD (the four at once, the one
   dataset drawn four ways inside the tracks). Pan/zoom, Fit, a record panel for any node — a record, a memory, an
   estate node, a loop step.

   What the rail's old context graph had lives here too (Notes/42 defect 48): the record panel shows the record's
   TEXT (the node's `text`, else `snippet` · `summary` · `content` · `preview`), its URL, tags, type and dataset beside
   relevance · tokens · who read it, with Focus turn · Include/Exclude · Open; the LIST drawer beside the plot lists
   the records as rows (source dot · label · relevance bar · tokens · included), the search box filters the list AND
   dims what does not match in the plot (n hits); the edge-type chips fold a relation type away (the old graph's edge
   classes); the frames scrubber walks the turns' snapshots the chat keeps (setFrames) without touching the live set.
   A frame is a TURN: {id, label, ts, mid, amid, turn, query, nodes, edges} — the context assembled for the question
   `mid` and what the response `amid` added (a node's `by`: 'u' in the prompt, 'a' read by the response). While a
   frame is active (setFrames(list, {active}) — the chat makes the turn in view the active one as the transcript
   scrolls; frame(id) — the scrubber, which the chat answers by scrolling to that turn), its included records are the
   focus, read by its mid / amid, and "turn m12" stands under the hub; the live set is what the host feeds setContext.

   <vera-context-graph>  API:
     setContext(nodes, edges, {focus:[ids], reads:{mid:[ids]}, stepReads:[[ids]], color:(source)=>css, turn})
     setMemory(nodes, edges, {color, edgeColor, hide}) · setDag(nodes, edges) · appendLoopEvent(ev) · setLoopEvents(evs)
     setPlan(goals) · setRuns(list, {current}) · setEstate(snapshot) · mix(family, level) · allEdges(on) · view(name)
     setFrames([{id, label, ts, nodes, edges}], {active}) · frame(id | null) · search(q) · list(on) · edgeType(name, on)
     fit() · select(id) · zoomTo(id, z) · positions() · state()
   events: vera:ctx:rendered {view, tokens, lit} · vera:ctx:pick {id} · vera:ctx:toggle {id} · vera:ctx:focus-turn {mid}
           · vera:ctx:run {session_id} · vera:ctx:collapse · vera:ctx:frame {id | null} · vera:ctx:alledges {on}
           · vera:ctx:toggle-all {included} · vera:ctx:preview {id, url}
   All edges: off draws only the relations that touch the prompt; on draws every relation AND every record's spoke to
   the hub (the aide retrieved each for the turn — the old graph's spokes), in the mini as in the element. The record
   panel: Zoom to (the plot pans to the record; a list row does it too), Preview page for a record with a URL
   (vera:ctx:preview — the host opens its browser pane), Activity evidence ↗ · Memory graph ↗ on a run node; the list's
   head: Incl all · Excl all (vera:ctx:toggle-all). The mini's card and rows carry the same data-a hooks:
   `[data-a="preview"][data-url]` · `[data-a="incl-all"]` · `[data-a="excl-all"]` · `a.lnk` (the run links).
   window.VeraContextGraph = { compute, mini, miniHtml, miniDetail, miniList, stateFrom, drawPlot, drawMiniGalaxy, drawLanes, recordCard,
   listHtml, VIEWS, FAMS, … } — compute() is pure (node-testable); mini(state, w, h) is the same layout in miniature
   (the tracks scaled to the box) and miniHtml(state, w, h, {detail, list, q}) draws it with the element's own classes —
   the `context_graph` widget form's M face; miniDetail(state, id, w) is the compact record card for one record (the
   overlay the form shows on a click) and miniList(state, {q, w, h}) the rows — the form's renderer handles the clicks:
   `.cg-node[data-id]` / `.cg-row[data-id]` → miniDetail(state, id) · `[data-a="toggle"][data-id]` → the include /
   exclude · `[data-a="list"]` → the list · `[data-a="close"]` → hide the card.   */
(function (root) {
  'use strict';
  // exposed for the host and the tests: what a record is
  const VIEWS = [['galaxy', 'Galaxy', 'angle is the source, distance is relevance'], ['iso', 'Iso', 'the galaxy on an isometric plate, relevance as height'],
    ['flow', 'Flow', 'a column per source, the most relevant on top'], ['time', 'Time', 'a column per turn that first read it, a lane per source'],
    ['quad', 'Quad', 'the four views at once — one dataset drawn four ways, inside the tracks']];
  // the plot's families beside the context (the GraphViews board's mixer): each is off · focus · all
  const FAMS = ['memory', 'loop', 'plan', 'estate'];
  const RAD = Math.PI / 180;
  const ORDER = ['vector', 'graph', 'fabric', 'web', 'news', 'ontology', 'cap', 'skill', 'run', 'related_qa', 'worldview', 'agent', 'entities', 'urls', 'both'];
  const DEF_COL = { vector: '#a78bfa', graph: '#fb923c', both: '#8fb87a', fabric: '#38bdf8', memory: '#5a9e8f', web: '#f59e0b', news: '#e879f9', cap: '#ec4899', run: '#60a5fa', skill: '#5a9e8f', ontology: '#c9955a', related_qa: '#e8a44c', worldview: '#2dd4bf', agent: '#888' };
  const tokOf = (n) => n.tok != null ? +n.tok : n.tokens != null ? +n.tokens : Math.max(12, Math.round(String(n.text || n.label || '').length / 4));
  // the record's text, as the chat feeds it: `text` (the rail's own field), else snippet · summary · content · preview
  const textOf = (n) => { if (!n) return ''; const v = [n.text, n.snippet, n.summary, n.content, n.preview].find((x) => x != null && String(x).trim() !== ''); return v == null ? '' : String(v); };
  const edgeTypeOf = (e) => String(e.label || e.type || e.relation || '').trim().toUpperCase() || 'RELATED';
  // a frame's reads: its included records, by the question (mid) or by the response (amid) as each node's `by` says
  const frameReads = (f, ns) => { const r = {}; const um = f.mid || f.label || '', am = f.amid || ''; ns.forEach((n) => { if (n.included === false) return; const key = n.by === 'a' && am ? am : um; if (!key) return; (r[key] = r[key] || []).push(n.id); }); return r; };
  // the typed icons (the Canvas board's node icons, 16-unit paths): a record wears the icon of what it is
  const ICON = {
    person: 'M8 8.4a2.8 2.8 0 1 0 0-5.6 2.8 2.8 0 0 0 0 5.6Zm-4.9 5.2a4.9 4.9 0 0 1 9.8 0',
    host: 'M2.4 3.6h11.2v6.8H2.4zM5.6 13h4.8M8 10.4V13',
    ssh: 'M2.4 3.4h11.2v9.2H2.4zM4.9 6.6l2 1.8-2 1.8M8.6 10.4h3',
    container: 'M8 2.5 13.5 5.2v5.6L8 13.5 2.5 10.8V5.2zM2.5 5.2 8 7.9l5.5-2.7M8 7.9v5.6',
    service: 'M8 3.2a4.8 4.8 0 1 0 0 9.6 4.8 4.8 0 0 0 0-9.6Zm0 3.1a1.7 1.7 0 1 1 0 3.4 1.7 1.7 0 0 1 0-3.4Z',
    file: 'M4.2 2.4h4.9l3 3v8.2H4.2zM9.1 2.4v3h3',
    commit: 'M8 5.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2ZM8 2.2v3.2M8 10.6v3.2',
    memory: 'M2.6 5 8 2.6 13.4 5 8 7.4zM2.6 8 8 10.4 13.4 8M2.6 11 8 13.4 13.4 11',
    skill: 'M8 2.5l1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11.2l-3.4 1.8.7-3.8L2.5 6.5l3.8-.5z',
    cap: 'M6.4 2.4v3.3M9.6 2.4v3.3M4.7 5.7h6.6v2.6a3.3 3.3 0 0 1-6.6 0zM8 11.6v2',
    step: 'M5.2 3.2 11 8l-5.8 4.8z',
    plan: 'M3.4 4.2h9.2M3.4 8h9.2M3.4 11.8h5.6',
    canvas: 'M2.6 3.2h10.8v9.6H2.6zM2.6 7.4h10.8M8 3.2v9.6',
    dataset: 'M8 2.6c3 0 5.2.8 5.2 1.8v7.2c0 1-2.2 1.8-5.2 1.8s-5.2-.8-5.2-1.8V4.4C2.8 3.4 5 2.6 8 2.6ZM2.8 4.4c0 1 2.2 1.8 5.2 1.8s5.2-.8 5.2-1.8',
    page: 'M3.2 2.6h9.6v10.8H3.2zM5.4 5.6h5.2M5.4 8h5.2M5.4 10.4h3.4',
    entity: 'M8 2.8l4.6 2.6v5.2L8 13.2l-4.6-2.6V5.4z',
    ontology: 'M8 2.6a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM3.6 9.4a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM12.4 9.4a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM8 6.6v1.4M6.4 8.9 4.6 9.6M9.6 8.9l1.8.7',
    agent: 'M8 7.6a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8ZM3.6 13.2c.5-2.6 2.2-3.9 4.4-3.9s3.9 1.3 4.4 3.9M10.8 4.4l1.6-1.6',
    qa: 'M3 3.4h10v6.4H6.2L3.8 12V9.8H3z'
  };
  // what a record is: its type, else its source, else what its label looks like (a path, a hash, a host)
  const kindOf = (n, source) => {
    const t = String(n.type || '').toLowerCase(), s = String(source || n.source || '').toLowerCase(), l = String(n.label || n.id || '');
    if (t === 'capability' || t === 'cap' || s === 'cap') return 'cap';
    if (t === 'dataset' || t === 'record' || s === 'fabric') return 'dataset';
    if (t === 'skill' || s === 'skill') return 'skill';
    if (t === 'agent' || s === 'agent') return 'agent';
    if (t === 'ontology') return 'ontology';
    if (t === 'entity' || s === 'entities') return 'entity';
    if (t === 'memory' || s === 'memory' || /memory|recall/.test(t)) return 'memory';
    if (t === 'page' || t === 'url' || s === 'web' || s === 'news' || s === 'urls') return 'page';
    if (t === 'step' || t === 'run' || s === 'run') return 'step';
    if (t === 'plan' || t === 'goal') return 'plan';
    if (t === 'canvas') return 'canvas';
    if (t === 'host' || t === 'node' || /^[a-z][a-z0-9-]*\.(int|lab|local)$/i.test(l)) return 'host';
    if (t === 'commit' || /^(commit\s+)?[0-9a-f]{7,40}$/i.test(l)) return 'commit';
    if (t === 'person') return 'person';
    if (s === 'related_qa' || t === 'qa') return 'qa';
    if (t === 'file' || t === 'chunk' || /\.[a-z0-9]{1,5}(\s|$|\b\d)/i.test(l.split(' ')[0]) || /\//.test(l)) return 'file';
    if (t === 'service') return 'service';
    return s === 'vector' ? 'file' : s === 'graph' ? 'entity' : 'dataset';
  };
  const iconSvg = (k) => ICON[k] ? '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><path d="' + ICON[k] + '"/></svg>' : '';
  const px = (v) => Math.round(v * 10) / 10;

  /* ── the layout, pure: state + size → everything the element draws ───────────────────────────────── */
  // a family's level in the mixer: an explicit setting, else the host's layersOff set (off), else everything
  const mixOf = (S, fam) => { const m = S.mix && S.mix[fam]; if (m === 'off' || m === 'focus' || m === 'all') return m; return (S.layersOff && S.layersOff.has(fam)) ? 'off' : 'all'; };
  // the rail's DAG graph as lane steps (the planned cap chain) with its own edges, through families.js when present
  const dagSteps = (nodes, edges) => {
    const F = root.VeraGraphFamilies; const list = Array.isArray(nodes) ? nodes : [];
    if (F && list.length) { const d = F.toDoc('dag', { nodes: list, edges: Array.isArray(edges) ? edges : [] }); if (!d.error && d.nodes.length) return { steps: d.nodes.map((n) => ({ id: n.id, label: n.label, status: n.status, cap: (n.wires || [])[0] || '', ms: '', parallel: n.kind === 'parallel' })), edges: d.edges.map((e) => ({ from: e.from, to: e.to })) }; }
    return { steps: list.map((n, i) => ({ id: 'dag:' + (n.id != null ? n.id : i), label: String(n.cap || n.label || n.id), status: n.status === 'done' ? 'ok' : n.status === 'running' ? 'running' : n.status === 'err' || n.status === 'error' ? 'fail' : 'pending', cap: n.out || '', ms: '' })), edges: [] };
  };
  // the memory ring's kind groups, in the order the arc lays them (the rail's record types; families.js names them)
  const MEM_KINDS = ['session', 'message', 'dag', 'dag_step', 'run', 'event', 'observation', 'fact', 'summary', 'entity'];
  const memKind = (t) => { t = String(t || 'memory').toLowerCase(); return t === 'dag_step' ? 'dag' : (MEM_KINDS.indexOf(t) >= 0 ? t : 'memory'); };
  const MEM_ORDER = ['session', 'message', 'dag', 'run', 'event', 'observation', 'fact', 'summary', 'entity', 'memory'];
  // the rail's edge kinds: the FOLLOWS spine is solid, everything else dashed (the memory graph panel's classes)
  const memEdgeCls = (rel) => { const r = String(rel || '').toUpperCase(); return /^(FOLLOWED_BY|FOLLOWS_ACTIVITY|NEXT_IN_SESSION|THEN)$/.test(r) ? 'spine' : /^(SESSION_CONTENT|CONTAINS|TRIGGERED_BY|TRIGGERED_BY_MSG|STARTS|CAP_RESULT)$/.test(r) ? 'hub' : ''; };
  const stOf = (s) => s === 'ok' || s === 'done' ? 'done' : s === 'running' ? 'run' : s === 'fail' ? 'fail' : 'pend';
  function compute(S, W, H) {
    const view = VIEWS.some((v) => v[0] === S.view) ? S.view : 'galaxy';
    const quad = view === 'quad';
    const tracks = S.tracks !== false;                          // a quad's cells draw no tracks of their own
    const k = S.mini ? (S.miniK || 0.3) : 1;                    // the mini: the same layout, the tracks scaled to the box
    const color = (s) => (S.color && S.color(s)) || DEF_COL[s] || '#8a7e70';
    // the frame in view: a turn's snapshot (the chat's CTX_FRAMES) stands in for the live records while it is picked
    const frames = S.frames || [];
    const frame = S.frame != null ? frames.find((f) => f && String(f.id) === String(S.frame)) : null;
    const nodes = ((frame ? frame.nodes : S.nodes) || []).filter((n) => n && n.id);
    // the relations by type (the old graph's edge classes: CITES · SIMILAR · HAS_SKILL · RELATED …), the folded types away
    const eOff = S.edgesOff || new Set();
    const edgesAll = ((frame ? frame.edges : S.edges) || []).filter((e0) => e0 && e0.from != null && e0.to != null).map((e0) => ({ from: e0.from, to: e0.to, label: String(e0.label || e0.type || e0.relation || '') }));
    const EDGES = edgesAll.filter((e0) => !eOff.has(edgeTypeOf(e0)));
    /* A RELATION TYPE HAS A COLOUR, and the edges are DRAWN in it — that is what makes the legend above a legend
       rather than a row of swatches next to uniformly grey lines. The name picks the hue, so a type is the same
       colour every render and on both graphs. The host's own edgeColor is NOT consulted here: it exists for the
       memory graph's relation styling and answers the same default for every context type, which collapsed the
       whole legend to one colour (measured: six bands, one distinct value). */
    const ETP = ['#a78bfa', '#5a9e8f', '#e8a44c', '#38bdf8', '#ec4899', '#8fb87a', '#fb923c', '#2dd4bf'];
    /* Colours come from the type's PLACE in the sorted set of types, not from a hash of its name: a hash
       collides, and two relation types sharing a colour is precisely the thing a legend must not do — measured,
       three types came out as two colours. Sorting by name (not by count) keeps a type's colour steady as counts
       move about, so the legend does not reshuffle itself between renders. */
    const etNames = [...new Set(edgesAll.map((e0) => edgeTypeOf(e0)))].sort();
    const etIdx = {}; etNames.forEach((t, i) => { etIdx[t] = i; });
    const etCol = (t) => ETP[(etIdx[t] == null ? etNames.length : etIdx[t]) % ETP.length];
    // the search: a record matches on its label, id, text, source, type or tags; what does not match dims in the plot
    const QS = String(S.q || '').trim().toLowerCase();
    const matchQ = (n) => !QS || [n.label, n.id, textOf(n), n.source, n.type, n.dataset, n.url].concat(n.tags || []).some((v) => v && String(v).toLowerCase().indexOf(QS) >= 0);
    const off = S.layersOff || new Set();
    const ghosts = S.related !== false;
    const lvl = {}; FAMS.forEach((f) => { lvl[f] = mixOf(S, f); });
    // the plan row: the run's own plan while a run is in the lane (the board's "planned workflow along the top"), else the goals
    const runPlan = (S.loop && S.loop.length && S.runPlan && S.runPlan.length) ? S.runPlan : [];
    const planIsRun = runPlan.length > 0;
    // the loop lane falls back to the run's DAG steps when no loop is live; the mixer's loop level governs both
    const loopAll = (S.loop && S.loop.length) ? S.loop : (S.dag || []);
    const loop = lvl.loop === 'off' ? [] : loopAll, isDag = loop.length > 0 && !(S.loop && S.loop.length);
    const plan = lvl.plan === 'off' ? [] : (planIsRun ? runPlan : (S.plan || []));
    // the estate: the snapshot's leaves (never the hub, a category or a monitor — code paths, not machines), each under its category
    const estAll = ((S.estate && S.estate.nodes) || []).filter((n) => n && !/^(hub|category|monitor)$/.test(n.kind || ''));
    const estFind = (name) => { const q = String(name).toLowerCase(); return estAll.find((n) => n.id.toLowerCase() === 'estate:' + q || String(n.label || '').toLowerCase() === q) || estAll.find((n) => String(n.label || '').toLowerCase().indexOf(q) >= 0 || n.id.toLowerCase().indexOf(q) >= 0); };
    const ranOnIds = new Set(); loop.forEach((s) => (s.ranOn || []).forEach((name) => { const n = estFind(name); if (n) ranOnIds.add(n.id); }));
    const estate = lvl.estate === 'off' ? [] : lvl.estate === 'focus' ? estAll.filter((n) => ranOnIds.has(n.id)) : estAll;
    const hasLanes = tracks && (loop.length > 0 || plan.length > 0 || estate.length > 0);
    const LANE_L = tracks && loop.length ? Math.round(118 * k) : 0, LANE_T = tracks && plan.length ? Math.round(52 * k) : 0, LANE_B = tracks && estate.length ? Math.round(68 * k) : 0;
    // the plot's own height ends above the estate strip; the pan/zoom centre stays the full plot's (the CSS origin)
    const PW = Math.max(quad ? 240 : 200 * k, W), PHfull = Math.max(quad ? 200 : 160 * k, H), PH = PHfull - LANE_B;
    // the plot proper: right of the loop lane, below the plan row
    const cxp = LANE_L + (PW - LANE_L) / 2, cyp = LANE_T + (PH - LANE_T) / 2;
    // in a frame the turn's prompt is the focus (its included records), read by the question or the response; the
    // live set keeps the host's focus and reads
    const focus = new Set(frame ? nodes.filter((n) => n.included !== false).map((n) => n.id) : (S.focus || []));
    const turnLabel = frame ? (frame.turn || 'turn ' + (frame.mid || frame.label || frame.id)) : (S.turn || '');
    // memory: the context's own memory records (injected) plus the session's memory graph (the rail's Memory tab) —
    // a session record already in the prompt is drawn once, filled; one never injected is hollow. In focus, only the
    // session records that touch something in the prompt stay on the arc
    const ctxIds = new Set(nodes.map((n) => n.id));
    // the level governs the RECALLS as much as the session's own records — it used to gate only the session's, so
    // turning memory off left the arm exactly as it was (Notes/42 defect 65). Off: nothing. Focus: the recalls this
    // prompt actually carries. All: every recall the turn assembled, injected or not.
    const memCtxAll = nodes.filter((n) => n.source === 'memory').map((n) => Object.assign({}, n, { _fam: 'memory', _injected: n.included !== false }));
    const memCtx = lvl.memory === 'off' ? [] : lvl.memory === 'focus' ? memCtxAll.filter((n) => n._injected) : memCtxAll;
    const litIds = new Set(nodes.filter((n) => focus.has(n.id) && n.included !== false).map((n) => n.id));
    const memTouch = new Set(); (S.memEdges || []).concat(EDGES).forEach((e) => { const a = String(e.from_id || e.from || ''), b = String(e.to_id || e.to || ''); if (litIds.has(a)) memTouch.add(b); if (litIds.has(b)) memTouch.add(a); });
    const memSessAll = lvl.memory === 'off' ? [] : (S.memory || []).filter((m) => m && m.id && !ctxIds.has(m.id)).map((m) => ({ id: m.id, label: (m.text || m.summary || m.capability || m.category || m.id || '').slice(0, 60), source: 'memory', type: m.record_type || m.type || 'memory', score: m.importance == null ? 0.5 : +m.importance, text: m.text || m.summary || '', included: false, rec: m, _fam: 'memory', _injected: false, _sess: true, created_at: m.created_at || '' }));
    const memSess = lvl.memory === 'focus' ? memSessAll.filter((m) => memTouch.has(m.id)) : memSessAll;
    const mem = memCtx.concat(memSess);
    /* A SOURCE has three levels, like a family: off · focus · all.
       FOCUS IS THE FEW THAT MATTER, not "the ones in the prompt". It was the latter, and that is a no-op in
       ordinary use — almost every assembled record has included !== false, so focus drew exactly what all drew
       and the control read as a plain on/off. It is the top FOC_N of the source by relevance now, which is what
       focus means everywhere else in this graph: the loop shows the running step and its neighbours, the plan
       the current step, memory the recalls that touch the prompt. A record that is explicitly selected stays
       whatever its rank, or picking one from the list could make it vanish.
       Anything never set still answers 'all' (or 'off' if the host put it in layersOff), so every existing
       caller behaves as before. */
    const FOC_N = 3;
    const srcLvl = (s) => mixOf(S, s || '?');
    const focRank = {};                                   // source -> the ids its focus keeps
    nodes.forEach((n) => { const s = n.source; if (s === 'memory' || srcLvl(s) !== 'focus') return;
      (focRank[s] = focRank[s] || []).push(n); });
    Object.keys(focRank).forEach((s) => { focRank[s] = new Set(focRank[s]
      .slice().sort((a, b) => (b.score == null ? 0.5 : +b.score) - (a.score == null ? 0.5 : +a.score))
      .slice(0, FOC_N).map((n) => n.id)); });
    const ctx = nodes.filter((n) => {
      if (n.source === 'memory') return false;
      const L = srcLvl(n.source);
      if (L === 'off') return false;
      if (L === 'focus' && !(focRank[n.source] && focRank[n.source].has(n.id)) && S.sel !== n.id) return false;
      return ghosts || n.included !== false;
    });
    const srcs = [...new Set(ctx.map((n) => n.source || '?'))].sort((a, b) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99));
    const reads = frame ? (frame.reads || frameReads(frame, nodes)) : (S.reads || {});
    const readBy = (id) => Object.keys(reads).filter((k2) => (reads[k2] || []).indexOf(id) >= 0);
    const stepReads = S.stepReads || [];
    const readBySteps = (id) => stepReads.map((r, i) => (r || []).indexOf(id) >= 0 ? i + 1 : 0).filter(Boolean);
    const turnKeys = Object.keys(reads);
    const firstRead = (id) => { const ks = readBy(id); return ks.length ? Math.min.apply(null, ks.map((k2) => turnKeys.indexOf(k2) + 1)) : 0; };
    // the families IN the plot (the GraphViews board: "the DAG run and the agentic loop each take a sector and draw their
    // own shape inside it; the plan sits at the top"): which steps and plan steps the mixer's level shows there
    const runIdx = (() => { let r = -1; loop.forEach((s, i) => { if (s.status === 'running') r = i; }); return r >= 0 ? r : loop.length - 1; })();
    const focusSteps = () => { const set = new Set(); const at = S.lsel != null && loop[S.lsel] ? S.lsel : runIdx; if (at < 0) return [];
      set.add(at); if (at > 0) set.add(at - 1); if (at + 1 < loop.length) set.add(at + 1);
      stepReads.forEach((ids, i) => { if (i < loop.length && (ids || []).some((id) => litIds.has(id))) set.add(i); });
      return [...set].sort((a, b) => a - b); };
    const focusPlan = () => { let at = plan.findIndex((p) => /^(running|active|in_progress)$/.test(p.status || '')); if (at < 0) at = plan.findIndex((p) => !/^(done|complete|completed|ok)$/.test(p.status || '')); if (at < 0) return []; return [at - 1, at, at + 1].filter((i) => i >= 0 && i < plan.length); };
    const plotLoop = loop.length && !quad ? (lvl.loop === 'focus' ? focusSteps() : loop.map((s, i) => i)) : [];
    const plotPlan = plan.length && !quad && (view === 'galaxy' || view === 'iso') ? (lvl.plan === 'focus' ? focusPlan() : plan.map((p, i) => i)) : [];
    const famPlot = plotLoop.length > 0 || plotPlan.length > 0;
    // the galaxy's bands: the context alone takes the whole circle; with a family beside it the context keeps the left
    // and top (190°), the plan the top right, the loop the right and bottom; the memory arm lies outside the context
    const CTX0 = famPlot ? -200 : -90, CTXW = famPlot ? 190 : 360;
    const step = CTXW / Math.max(srcs.length, 1);
    const midOf = (si) => famPlot ? CTX0 + (si + 0.5) * step : -90 + si * step;
    const MEM_A0 = famPlot ? -195 : -150, MEM_AW = famPlot ? 180 : 300;
    const PLAN_A = [-10, 40], LOOP_A = [40, 160];
    // the iso view: the galaxy at a tilt, sized from the column. The plate is the ground square [-s, s]² about the hub
    // (s = RMAX + 40·k, drawn 5% larger), which the projection turns into a rhombus 2·PL·kx·s wide and 2·PL·ky·s tall,
    // centred Y0 below the hub; it is the widest thing on the floor — the memory ring (RMAX + 44·k, inscribed), the
    // discs and the sector labels all lie inside it — so it is the PLATE's bounding box that must fit the plot, a
    // clearance from the plot's edge in place of the galaxy's label padding (which the plate encloses here): the tilt
    // is that box's aspect (steeper on a tall column, 30° on a wide one, so the rhombus fills the box) and the size
    // follows from whichever side binds; the stems and the hub's pin rise above the floor inside the rhombus, the 2·Y0
    // the centre's offset leaves above the plate keeps the stand from crowding the plan row
    let RMAX, isoP = null, ISO = null;
    const memMargin = (mem.length ? 96 : 62) * k;
    if (view === 'iso') {
      const K0 = 0.78 * Math.SQRT2, AZ = Math.SQRT1_2, Y0 = 40 * k, PL = 2 * 1.05;      // k = 1.103 at azimuth 45° is the plate's 0.78 / 0.39 at a 30° tilt
      const PAD = 20 * k;                                                                 // the plate's clearance from the plot's edge
      const halfX = Math.max(1, (PW - LANE_L) / 2 - PAD), halfY = Math.max(1, (PH - LANE_T) / 2 - PAD - Y0);
      const sinT = Math.max(0.5, Math.min(0.85, halfY / halfX));
      const kx = K0 * AZ, ky = K0 * AZ * sinT, tilt = Math.asin(sinT) * 180 / Math.PI;
      const s = Math.min(halfX / (PL * kx), halfY / (PL * ky));                          // the plate's half-side that fits both ways
      RMAX = Math.max(60 * k, s - 40 * k);
      const S1 = RMAX + 40 * k, box = { w: 2 * PL * kx * S1, h: 2 * PL * ky * S1 };       // the plate's projected bounding box (RMAX may have hit its floor)
      const P = typeof S.isoMake === 'function' ? S.isoMake(tilt, K0) : null;
      isoP = P ? (x, y, z) => { const p = P(x - cxp, y - cyp, z || 0); return { x: cxp + p[0], y: cyp + Y0 + p[1] }; }
        : (x, y, z) => { const u = x - cxp, v = y - cyp; return { x: cxp + (u - v) * kx, y: cyp + Y0 + (u + v) * ky - (z || 0) }; };
      ISO = { kx, ky, sinT, tilt, y0: Y0, k: K0, pad: PAD, box, fit: Math.max(box.w / (PW - LANE_L), box.h / (PH - LANE_T)) };
    } else RMAX = Math.max(60 * k, Math.min(PW - LANE_L, PH - LANE_T) / 2 - memMargin);
    const out = { view, rings: [], spokes: [], sectorLabels: [], cnodes: [], memNodes: [], memLabels: [], cedges: [], sedges: [], stems: [], pins: [], plate: null, regions: [], loopNodes: [], loopStems: [], planNodes: [], stepNodes: [], planPlot: [], estNodes: [], estLabels: [], dividers: [], pos: {}, tokens: 0, lit: 0, hits: 0, q: QS, turn: turnLabel, hub: { x: cxp, y: cyp, hid: view === 'flow' || view === 'time' || quad }, discs: [], iso: ISO, k };
    // the plot's pan/zoom, for what is drawn OUTSIDE it (the lanes) but joins a record inside it
    const GZ = (S.pan && S.pan.z) || 1, GX = (S.pan && S.pan.x) || 0, GY = (S.pan && S.pan.y) || 0;
    const atP = (p) => ({ x: PW / 2 + (p.x - PW / 2) * GZ + GX, y: PHfull / 2 + (p.y - PHfull / 2) * GZ + GY });
    const edge = (list, a, b, col, cls, title) => { const dx = b.x - a.x, dy = b.y - a.y; list.push({ x: px(a.x), y: px(a.y), len: px(Math.sqrt(dx * dx + dy * dy)), deg: +(Math.atan2(dy, dx) * 180 / Math.PI).toFixed(2), col, cls, title }); };
    const stemTo = (x, y, z) => { const p = isoP(x, y, z), g = isoP(x, y, 0); out.stems.push({ x: px(p.x), y: px(p.y), h: px(Math.max(0, g.y - p.y)) }); return p; };
    const lpos = [], spos = [];                                   // the lane's pills (the track) and the plot's steps (the layer)
    /* ── the QUAD view: the one dataset drawn four ways inside the tracks ── */
    if (quad) {
      const cells = [['galaxy', 0, 0], ['iso', 1, 0], ['flow', 0, 1], ['time', 1, 1]];
      const cw = (PW - LANE_L) / 2, ch = (PH - LANE_T) / 2;
      const sub = Object.assign({}, S, { tracks: false, pan: null, isoMake: S.isoMake });
      const shift = (list, dx, dy, keys) => list.forEach((o) => { keys.forEach((kk) => { if (o[kk] != null) o[kk] = px(+o[kk] + (kk === 'x' || kk === 'cx' || kk === 'lx' ? dx : dy)); }); });
      cells.forEach((c, ci) => { const dx = LANE_L + c[1] * cw, dy = LANE_T + c[2] * ch; const o = compute(Object.assign({}, sub, { view: c[0] }), cw, ch);
        const dup = ci > 0 ? ' dup' : '';
        ['rings', 'spokes', 'sectorLabels', 'cnodes', 'memNodes', 'memLabels', 'cedges', 'stems', 'pins', 'discs', 'regions', 'stepNodes', 'planPlot'].forEach((key) => shift(o[key], dx, dy, ['x', 'y', 'cx', 'cy', 'lx', 'ly']));
        if (o.plate) { o.plate.x = px(o.plate.x + dx); o.plate.y = px(o.plate.y + dy); }
        o.cnodes.forEach((n) => { n.cls += dup; }); o.memNodes.forEach((n) => { n.cls += dup; }); o.stepNodes.forEach((n) => { n.cls += dup; });
        o.sedges.forEach((e2) => { e2.x = px(e2.x + dx); e2.y = px(e2.y + dy); out.cedges.push(e2); });     // a cell's in-plot relations move with the plot
        ['rings', 'spokes', 'sectorLabels', 'cnodes', 'memNodes', 'memLabels', 'cedges', 'stems', 'pins', 'discs', 'regions', 'stepNodes', 'planPlot'].forEach((key) => { out[key] = out[key].concat(o[key]); });
        if (o.plate) out.plate = out.plate || o.plate;
        if (ci === 0) { Object.keys(o.pos).forEach((id) => { const p = o.pos[id]; out.pos[id] = Object.assign({}, p, { x: p.x + dx, y: p.y + dy }); }); out.tokens = o.tokens; out.lit = o.lit; out.hubs = [{ x: o.hub.x + dx, y: o.hub.y + dy, hid: o.hub.hid }]; }
        else if (o.hub && !o.hub.hid) out.hubs.push({ x: o.hub.x + dx, y: o.hub.y + dy, hid: false });
        out.regions.push({ x: px(dx + 6), y: px(dy + 4), col: 'var(--cg-t3)', t: c[0], cell: true }); });
      out.dividers = [{ x: px(LANE_L + cw), y: px(LANE_T), w: 1, h: px(PH - LANE_T) }, { x: px(LANE_L), y: px(LANE_T + ch), w: px(PW - LANE_L), h: 1 }];
      out.hub = { x: out.hubs[0].x, y: out.hubs[0].y, hid: true };
    }
    const bucket = {};
    /* THE ARM. Distance was a function of SCORE ALONE, through a curve that saturated:
           r = min(RMAX, 52k + (1 - score)·(RMAX - 52k)/0.4)
       (1 - score)/0.4 reaches 1 at score 0.6, so every record scoring 0.6 or less sat on RMAX exactly — and a
       record with no score at all defaults to 0.5, which is most of them (memory, fabric, caps, skills and
       ontologies carry no relevance number). The result was a dense ring of touching circles at the rim, which
       is what "nodes of the same type stacked one atop the other" is. Only scores between 0.6 and 1.0 moved at
       all, so the radius carried almost no information.
       Two changes:
       - RANK BREAKS THE TIE. Each record also takes its place in its family's own order (f, the sorted index),
         so equal scores still spread along the arm instead of collapsing onto one radius. Score still leads;
         rank only separates.
       - THE ARM CURVES. The angle now advances with distance, which is what makes a spiral rather than the
         spokes-and-rings radar this drew before. SWIRL is the turn across the whole arm; a family's band leaves
         its sector as an arc, and the arms read as arms.
       The centre is kept clear (R_IN) so the hub and its label are not crowded, and the fan is inset by the
       widest node's radius so neighbouring families do not touch at the rim. */
    const R_IN = Math.min(RMAX * 0.30, 46 * k);
    const SWIRL = 34;
    /* One radius law, so the guide rings mean what the nodes mean (f = rank in family, 0.5 = rank-neutral).
       SCORE KEEPS ITS OLD SENSITIVITY. (1 - score)/0.4 magnifies differences across 0.6–1.0, which is the band
       real relevance scores land in; flattening it over the whole 0–1 range pulled the top-scored records of a
       family together and measured WORSE than what it replaced (28.8px apart before, 16px after). It is kept.
       RANK ONLY BREAKS THE TIE. That curve saturates at score 0.6, and a record with no score defaults to 0.5 —
       which is most of them — so everything unscored landed on RMAX exactly, one ring of touching circles. The
       rank term is small enough not to disturb genuinely-scored records and large enough to fan out the tied
       ones, which is the whole of what it is for. */
    /* rw is how much of the arm RANK gets, and it is decided per family by whether score has anything to say.
       A family whose records all carry the same score (or none) gets rank almost all of the arm — there is
       nothing else to order them by, and leaving them on 28% of it packed 24 records into 53px, which is the
       crowding. A family with a real score spread keeps score in charge and rank only nudges ties apart. */
    const armR = (score, f, rw) => R_IN + (RMAX - R_IN)
      * Math.max(0, Math.min(1, (1 - rw) * Math.min(1, Math.max(0, 1 - score) / 0.4) + rw * f));
    const place = (si, i, n, score, id, rw) => {
      const mid = midOf(si);
      /* The turn is spent INSIDE the family's own sector, not added to it. The sectors mean something — the
         context keeps the left and top, the plan the top right, the loop the right and bottom — and a swirl
         laid on top of the fan pushed context records round into the loop's arc. So the fan gives up the room
         the turn needs, and the turn is centred on the sector's middle: the arm curves, the family stays put. */
      const swirl = Math.min(SWIRL, Math.max(0, step * 0.45));
      const half = Math.max(3, (step / 2 - 6) - swirl / 2);
      const f = n > 1 ? i / (n - 1) : 0.5;                    // this record's rank within its family
      const r = armR(score, f, rw);
      const arm = (r - R_IN) / Math.max(1, RMAX - R_IN);       // 0 at the core, 1 at the rim
      /* The fan widens as the radius shrinks. A fixed angular step is NOT a fixed distance: the gap between two
         neighbours is r·Δa, so the same fan that reads comfortably at the rim crushes the records nearest the
         core. Widening by RMAX/r holds the gap roughly constant along the arm; the sector is still the ceiling,
         so a family never spills into its neighbour. */
      // fan across the sector. Scaling this by RMAX/r to widen the crowded inner rows and clamping it back into
      // the sector was tried and measured WORSE: every over-scaled node lands on the same sector edge, two
      // records at identical angle and radius, 0.0px apart.
      const spread = n === 1 ? 0 : -half + (i + 0.5) * ((half * 2) / n);
      const a = mid + spread + (swirl * arm - swirl / 2);
      const gx = cxp + Math.cos(a * RAD) * r, gy = cyp + Math.sin(a * RAD) * r;
      if (view === 'galaxy') return { x: gx, y: gy };
      if (view === 'iso') return stemTo(gx, gy, score * 70 * k);
      if (view === 'flow') { const cw = (PW - LANE_L - 24 * k) / Math.max(srcs.length, 1); return { x: LANE_L + 12 * k + cw * (si + 0.5), y: LANE_T + 44 * k + Math.min(1, (1 - score) / 0.4) * (PH - LANE_T - (150 + (plotLoop.length ? 50 : 0)) * k) }; }
      const cols = Math.max(turnKeys.length, 1) + 1, k2 = firstRead(id), cw = (PW - LANE_L - 60 * k) / cols, lh = (PH - LANE_T - 120 * k) / Math.max(srcs.length + (plotLoop.length ? 1 : 0), 1);
      const key = k2 + ':' + si, nth = (bucket[key] = (bucket[key] || 0) + 1) - 1;
      return { x: LANE_L + 30 * k + cw * (k2 ? k2 - 0.5 : cols - 0.5) + (nth % 2) * 10 * k, y: LANE_T + 50 * k + lh * si + nth * 19 * k };
    };
    if (!quad) {
    // the relevance guides read off the SAME law the nodes follow, rank-neutral — they drifted apart when the
    // radius changed and would otherwise mark bands nothing sits in
    if (view === 'galaxy') [0.90, 0.75, 0.60].forEach((sc) => { out.rings.push({ cx: px(cxp), cy: px(cyp), d: px(armR(sc, 0.5, 0.28) * 2) }); });
    if (view === 'time') { const cols = Math.max(turnKeys.length, 1) + 1, cw = (PW - LANE_L - 60 * k) / cols; for (let c = 1; c <= cols; c++) out.sectorLabels.push({ name: c < cols ? (turnKeys[c - 1] || 'm' + c) : 'never', col: 'var(--cg-t3)', x: px(LANE_L + 30 * k + cw * (c - 0.5)), y: px(LANE_T + 12 * k) }); }
    if (view === 'iso') { const c = isoP(cxp, cyp, 0); out.plate = { x: px(c.x - ISO.box.w / 2), y: px(c.y - ISO.box.h / 2), w: px(ISO.box.w), h: px(ISO.box.h) };
      // the sector discs (the board's annular wedges on the floor): one per family, over the family's sector and the
      // relevance radii its nodes can take; a ground circle under this projection is the axis-aligned ellipse
      // kx·√2 × ky·√2 about the hub, its parameter phased by 45° — so the wedge is a conic gradient in a circle, then that scale
      const r0 = 44 * k, r1 = RMAX + 6 * k, kx = ISO.kx * Math.SQRT2, ky = ISO.ky * Math.SQRT2, tf = 'scale(' + kx.toFixed(3) + ',' + ky.toFixed(3) + ')';
      const disc = (a0, aw, col, name, ri, ro) => out.discs.push({ x: px(c.x - ro), y: px(c.y - ro), d: px(ro * 2), col, a0: (a0 + 90 + 45).toFixed(1) + 'deg', a1: aw.toFixed(1) + 'deg', m0: (ri / ro * 50).toFixed(1) + '%', m1: (ri / ro * 50 + 0.6).toFixed(1) + '%', tf, name });
      srcs.forEach((s2, si) => { const mid = midOf(si), half = step / 2 - 5; disc(mid - half, half * 2, color(s2), s2, r0, r1); });
      if (plotPlan.length) disc(PLAN_A[0], PLAN_A[1] - PLAN_A[0], 'var(--cg-t2)', 'plan', RMAX * 0.62, r1);
      if (plotLoop.length) disc(LOOP_A[0], LOOP_A[1] - LOOP_A[0], 'var(--cg-ac)', isDag ? 'dag' : 'loop', r0, r1);
      // the memory ring lies on the floor too (dashed), and the hub stands on a pin
      if (mem.length) { const R1 = RMAX + 44 * k; out.rings.push({ cx: px(c.x), cy: px(c.y), d: px(R1 * 2), cls: 'mem iso', tf }); }
      const hz = 40 * k; const top = isoP(cxp, cyp, hz); out.hub = { x: top.x, y: top.y, hid: false }; out.pins.push({ x: px(c.x), y: px(top.y), h: px(Math.max(0, c.y - top.y)), col: 'var(--cg-ac)', cls: 'hub' }); }
    srcs.forEach((s, si) => {
      const list = ctx.filter((n) => (n.source || '?') === s).sort((a, b) => (b.score || 0) - (a.score || 0));
      const mid = midOf(si), col = color(s);
      if (view === 'galaxy') { out.spokes.push({ x: px(cxp), y: px(cyp), len: px(RMAX + 10 * k), deg: (mid - step / 2) }); if (famPlot && si === srcs.length - 1) out.spokes.push({ x: px(cxp), y: px(cyp), len: px(RMAX + 10 * k), deg: (mid + step / 2) }); out.sectorLabels.push({ name: s, col, x: px(cxp + Math.cos(mid * RAD) * (RMAX + 26 * k)), y: px(cyp + Math.sin(mid * RAD) * (RMAX + 26 * k)) }); }
      else if (view === 'iso') { const p = isoP(cxp + Math.cos(mid * RAD) * (RMAX + 26 * k), cyp + Math.sin(mid * RAD) * (RMAX + 26 * k), 0); out.sectorLabels.push({ name: s, col, x: px(p.x), y: px(p.y) }); }
      else if (view === 'flow') { const cw = (PW - LANE_L - 24 * k) / srcs.length; out.sectorLabels.push({ name: s, col, x: px(LANE_L + 12 * k + cw * (si + 0.5)), y: px(LANE_T + 26 * k) }); }
      else { const lh = (PH - LANE_T - 120 * k) / (srcs.length + (plotLoop.length ? 1 : 0)); out.sectorLabels.push({ name: s, col, x: px(LANE_L + 8 * k), y: px(LANE_T + 50 * k + lh * si), lane: true }); }
      /* Half the arm to rank, half to score. The list is already sorted by score, so rank PRESERVES the score
         ordering — it only guarantees that records the score cannot tell apart still get their own radius.
         Measured at the density a real turn produces (48 records over 8 families): the closest pair is unchanged
         at 27px, families sit slightly further apart, nothing overlaps, and every record in a family gains its
         own radius where three in six used to share one.
         KNOWN LIMIT: at extreme density — two dozen records in ONE family — this is worse than the flat ring it
         replaced (pairs under 20px: 76 -> 112). A single ring is the most space-efficient way to place many tied
         records; it is simply unreadable, which is the reason for changing it. The proper answer there is bands
         whose capacity follows their own radius, the way the memory ring packs its rows; banding by a capacity
         taken from RMAX was tried and is worse still, because the inner bands are far too short for it (nine
         records at r=46 land 3px apart). That work is not done here. */
      const sOf = (x) => Math.max(0, Math.min(1, x.score == null ? 0.5 : +x.score));
      const rw = 0.5;
      list.forEach((n, i) => {
        const score = sOf(n), tok = tokOf(n);
        const q = place(si, i, list.length, score, n.id, rw);
        const inFocus = focus.has(n.id) && n.included !== false, by = readBy(n.id), everRead = by.length > 0, ghost = n.included === false;
        if (inFocus) { out.tokens += tok; out.lit++; }
        const d = Math.max(17 * k, (7 + Math.sqrt(tok) / 2.7 + 8) * k);
        const kind = kindOf(n, s);
        const hit = matchQ(n); if (QS && hit) out.hits++;
        out.pos[n.id] = { x: q.x, y: q.y, col, source: s, label: n.label || n.id, lit: inFocus, rim: d / 2, ghost, score, tok, kind: n.type || s, icon: kind, rec: n, hit, by: n.by === 'a' ? 'a' : 'u' };
        out.cnodes.push({ id: n.id, x: px(q.x), y: px(q.y), d: px(d), col, kind, cls: (inFocus ? 'lit ' : everRead ? '' : 'dim ') + (ghost ? 'ghost ' : '') + (n.type === 'dataset' ? 'sq ' : '') + (QS && !hit ? 'miss ' : '') + (S.sel === n.id ? 'on' : ''),
          op: (inFocus ? 1 : everRead ? 0.55 + score * 0.3 : ghost ? 0.4 : 0.55).toFixed(2),
          title: (n.label || n.id) + ' · ' + s + (n.type ? ' · ' + n.type : '') + ' · relevance ' + score.toFixed(2) + ' · ' + tok + ' tokens' + (ghost ? ' · related, not injected' : inFocus ? ' · in this prompt' : everRead ? ' · read by ' + by.join(', ') : '') });
      });
    });
    if (ctx.length && (view === 'galaxy' || view === 'iso')) out.regions.push({ x: px(cxp - 26 * k), y: px(view === 'iso' ? PH - 26 * k : Math.min(PH - 16 * k, cyp + RMAX + 40 * k)), col: 'var(--cg-t3)', t: 'context' });
    // the loop as a layer of the plot: a chain along its sector (galaxy), a standing column of steps at the sector's
    // foot (iso), a lane across the foot (flow), a row of its own (time); a sub-step further out, a branch beside
    if (plotLoop.length) {
      const n = plotLoop.length, lcol = 'var(--cg-ac)';
      const at = (i, j) => { const s = loop[i]; const sub = s.sub != null && s.sub >= 0, br = !!(s.branch && !/^sub:/.test(s.branch));
        if (view === 'galaxy') { const a = LOOP_A[0] + (j + 0.5) * (LOOP_A[1] - LOOP_A[0]) / n, r = RMAX * (0.5 + (sub ? 0.22 : 0) + (br ? 0.14 : 0)); return { x: cxp + Math.cos(a * RAD) * r, y: cyp + Math.sin(a * RAD) * r }; }
        if (view === 'iso') { const a = (LOOP_A[0] + LOOP_A[1]) / 2 * RAD, r = RMAX * 0.72; const fx = cxp + Math.cos(a) * r + (sub ? 14 : 0) * k + (br ? 22 : 0) * k, fy = cyp + Math.sin(a) * r; const dz = Math.min(16 * k, (PH - LANE_T - 120 * k) / (n + 1)); return stemTo(fx, fy, dz * (j + 1)); }
        if (view === 'flow') { const cw = (PW - LANE_L - 60 * k) / n; return { x: LANE_L + 30 * k + cw * (j + 0.5), y: PH - (mem.length ? 96 : 60) * k + (sub ? 16 : 0) * k + (br ? -16 : 0) * k }; }
        const cw = (PW - LANE_L - 60 * k) / n, lh = (PH - LANE_T - 120 * k) / (srcs.length + 1); return { x: LANE_L + 30 * k + cw * (j + 0.5), y: LANE_T + 50 * k + lh * srcs.length + (sub ? 12 : 0) * k }; };
      if (view === 'iso') { const a = (LOOP_A[0] + LOOP_A[1]) / 2 * RAD, r = RMAX * 0.72; const foot = isoP(cxp + Math.cos(a) * r, cyp + Math.sin(a) * r, 0); const dz = Math.min(16 * k, (PH - LANE_T - 120 * k) / (n + 1)); const top = isoP(cxp + Math.cos(a) * r, cyp + Math.sin(a) * r, dz * (n + 1)); out.pins.push({ x: px(foot.x), y: px(top.y), h: px(Math.max(0, foot.y - top.y)), col: lcol, cls: 'col' }); }
      plotLoop.forEach((i, j) => { const s = loop[i]; const q = at(i, j); spos[i] = q; const st = stOf(s.status);
        out.stepNodes.push({ i, id: s.id, x: px(q.x), y: px(q.y), cls: st + (S.lsel === i ? ' sel' : '') + (s.sub != null && s.sub >= 0 ? ' sub' : '') + (s.pruned ? ' pruned' : ''), label: (i + 1) + ' ' + String(s.label || s.id).slice(0, 14), title: 'loop step ' + (i + 1) + ' · ' + (s.label || s.id) + ' · ' + (s.status || 'pending') + (s.cap ? ' · ' + s.cap : '') + ' · click to see what it read' }); });
      // THEN: the chain (a sub-step from its parent), lit up to the running step
      plotLoop.forEach((i) => { const s = loop[i]; const from = s.sub != null && s.sub >= 0 ? s.sub : (i > 0 ? i - 1 : -1); if (from >= 0 && spos[from] && spos[i]) edge(out.cedges, spos[from], spos[i], lcol, 'then' + (loop[i].status === 'running' || loop[i].status === 'ok' ? ' lit' : ''), 'step ' + (from + 1) + ' → step ' + (i + 1)); });
      if (isDag) (S.dagEdges || []).forEach((e2) => { const a = loop.findIndex((s) => s.id === e2.from), b = loop.findIndex((s) => s.id === e2.to); if (a >= 0 && b >= 0 && spos[a] && spos[b] && !(b === a + 1)) edge(out.cedges, spos[a], spos[b], lcol, 'then', loop[a].label + ' → ' + loop[b].label); });
      if (view === 'galaxy') { const a = (LOOP_A[0] + LOOP_A[1]) / 2; out.sectorLabels.push({ name: isDag ? 'dag' : 'loop', col: lcol, x: px(cxp + Math.cos(a * RAD) * (RMAX + 26 * k)), y: px(cyp + Math.sin(a * RAD) * (RMAX + 26 * k)) }); }
      else if (view === 'iso') { const a = (LOOP_A[0] + LOOP_A[1]) / 2; const p = isoP(cxp + Math.cos(a * RAD) * (RMAX + 26 * k), cyp + Math.sin(a * RAD) * (RMAX + 26 * k), 0); out.sectorLabels.push({ name: isDag ? 'dag · a column' : 'loop · a column', col: lcol, x: px(p.x), y: px(p.y) }); }
      else if (view === 'flow') out.sectorLabels.push({ name: isDag ? 'dag' : 'loop', col: lcol, x: px(LANE_L + 8 * k), y: px(PH - (mem.length ? 96 : 60) * k - 16 * k), lane: true });
      else { const lh = (PH - LANE_T - 120 * k) / (srcs.length + 1); out.sectorLabels.push({ name: isDag ? 'dag' : 'loop', col: lcol, x: px(LANE_L + 8 * k), y: px(LANE_T + 50 * k + lh * srcs.length), lane: true }); }
    }
    // the plan as a layer: its sector at the top right (galaxy and iso), a diamond per step, NEXT along the arc
    if (plotPlan.length) {
      const n = plotPlan.length; let prev = null, prevI = -1;
      plotPlan.forEach((i, j) => { const p = plan[i]; const a = (PLAN_A[0] + (j + 0.5) * (PLAN_A[1] - PLAN_A[0]) / n) * RAD, r = RMAX * 0.8; const gx = cxp + Math.cos(a) * r, gy = cyp + Math.sin(a) * r; const q = view === 'iso' ? isoP(gx, gy, 0) : { x: gx, y: gy };
        const st = /^(done|complete|completed|ok)$/.test(p.status || '') ? 'done' : /^(running|active|in_progress)$/.test(p.status || '') ? 'run' : p.status === 'fail' ? 'fail' : 'pend';
        out.planPlot.push({ i, x: px(q.x), y: px(q.y), cls: st, label: String(p.label || p.id).slice(0, 18), title: 'plan step ' + (i + 1) + ' · ' + (p.label || p.id) + ' · ' + (p.status || '') });
        if (prev) edge(out.cedges, prev, q, 'var(--cg-t3)', 'next', 'plan step ' + (prevI + 1) + ' → ' + (i + 1)); prev = q; prevI = i;
        // EXECUTED_BY inside the plot: the plan step to the loop steps that ran it, when those are in the plot
        (planIsRun ? (p.steps || []) : []).forEach((si) => { if (spos[si]) edge(out.cedges, q, spos[si], st === 'run' ? 'var(--cg-ac)' : 'var(--cg-bd2)', 'exec' + (st === 'run' ? ' lit' : ''), 'plan step ' + (i + 1) + ' ran as loop step ' + (si + 1)); }); });
      const a = (PLAN_A[0] + PLAN_A[1]) / 2 * RAD; const lp = view === 'iso' ? isoP(cxp + Math.cos(a) * (RMAX + 26 * k), cyp + Math.sin(a) * (RMAX + 26 * k), 0) : { x: cxp + Math.cos(a) * (RMAX + 26 * k), y: cyp + Math.sin(a) * (RMAX + 26 * k) };
      out.sectorLabels.push({ name: 'plan', col: 'var(--cg-t2)', x: px(lp.x), y: px(lp.y) });
    }
    // memory on the outer arc — THE MEMORY RING (the board): never strewn about; one arm, the injected recalls on the
    // inner arc, the session's records outside it grouped by kind (session · message · dag · event · fact · summary ·
    // entity …), ordered in time within a group, hollow where never injected, in by importance
    if (mem.length) {
      const R1 = RMAX + 44 * k, R2 = RMAX + 74 * k, ROW = 26 * k, GAP = 1;
      const memAt = (a, R, row) => { if (view === 'galaxy') return { x: cxp + Math.cos(a * RAD) * R, y: cyp + Math.sin(a * RAD) * R }; if (view === 'iso') return isoP(cxp + Math.cos(a * RAD) * R, cyp + Math.sin(a * RAD) * R, 0); return { x: LANE_L + 40 * k + ((a - MEM_A0) / MEM_AW) * (PW - LANE_L - 80 * k), y: PH - 50 * k + row * 20 * k }; };
      const byTime = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || ''));
      const inj = mem.filter((n) => n._injected).sort(byTime), gh = ghosts ? mem.filter((n) => !n._injected).sort(byTime) : [];
      const memCol = (n) => (n._sess && S.memColor && S.memColor(n.type)) || color('memory');
      const shape = (n) => n.type === 'message' ? 'msg' : n.type === 'session' ? 'sess' : /^dag/.test(n.type || '') ? 'dag' : '';
      const put = (n, a, R, row, ghost) => { const q = memAt(a, R, row); const lit = !ghost && focus.has(n.id); const imp = Math.max(0, Math.min(1, +n.score || 0));
        out.pos[n.id] = { x: q.x, y: q.y, col: memCol(n), source: 'memory', label: n.label || n.id, lit, rim: 6 * k, ghost, score: imp, tok: tokOf(n), kind: n.type || 'memory', rec: n.rec || n, sess: !!n._sess, by: 'u' }; if (lit) { out.tokens += tokOf(n); out.lit++; }
        const hit = matchQ(n); if (QS && hit) out.hits++; out.pos[n.id].hit = hit;
        out.memNodes.push({ id: n.id, x: px(q.x), y: px(q.y), col: memCol(n), kind: memKind(n.type), op: ghost ? (0.45 + imp * 0.45).toFixed(2) : '1', cls: shape(n) + ' ' + (ghost ? 'ghost ' : lit ? 'lit ' : '') + (QS && !hit ? 'miss ' : '') + (S.sel === n.id ? 'on' : ''), title: (n.label || n.id) + ' · ' + (n.type || 'memory') + (ghost ? ' · in the session, not injected' : lit ? ' · in this prompt' : ' · injected') + (n._sess ? ' · importance ' + imp.toFixed(2) : '') }); };
      // the injected arc: evenly along the arm
      inj.forEach((n, i) => { const a = inj.length === 1 ? MEM_A0 + MEM_AW / 2 : MEM_A0 + i * (MEM_AW / (inj.length - 1)); put(n, a, R1, 0, false); });
      // the session's records: kind groups along the arm, each group's share by its count (a slot between groups); a row
      // holds as many as the arc's length allows, further rows lie outside
      if (gh.length) { const groups = []; const gi = {}; gh.forEach((n) => { const g = memKind(n.type); if (gi[g] == null) { gi[g] = groups.length; groups.push({ kind: g, items: [] }); } groups[gi[g]].items.push(n); });
        groups.sort((a, b) => MEM_ORDER.indexOf(a.kind) - MEM_ORDER.indexOf(b.kind));
        const arcLen = (view === 'galaxy' || view === 'iso') ? MEM_AW * RAD * R2 : (PW - LANE_L - 80 * k); const PER = Math.max(6, Math.floor(arcLen / (16 * k)));
        const rows = Math.max(1, Math.ceil((gh.length + GAP * (groups.length - 1)) / PER)); const perRow = Math.ceil((gh.length + GAP * (groups.length - 1)) / rows);
        let slot = 0; const slotA = (s2) => MEM_A0 + ((s2 % perRow) + 0.5) * (MEM_AW / perRow), slotR = (s2) => Math.floor(s2 / perRow);
        groups.forEach((g, gi2) => { const s0 = slot; g.items.forEach((n) => { put(n, slotA(slot), R2 + slotR(slot) * ROW, 1 + slotR(slot), true); slot++; });
          const am = (slotA(s0) + slotA(slot - 1)) / 2, r = R2 + (rows) * ROW - 6 * k; const lq = (view === 'galaxy' || view === 'iso') ? memAt(am, r, 0) : { x: memAt(am, 0, 0).x, y: PH - 62 * k };
          out.memLabels.push({ x: px(lq.x), y: px(lq.y), t: g.kind + ' · ' + g.items.length, col: (S.memColor && S.memColor(g.kind === 'dag' ? 'dag' : g.kind)) || 'var(--cg-ac2)' }); slot += GAP; }); }
      if (view === 'galaxy') out.rings.push({ cx: px(cxp), cy: px(cyp), d: px(R1 * 2), cls: 'mem' });
      out.regions.push(view === 'galaxy' ? { x: px(Math.min(PW - 60 * k, cxp + R1 - 10 * k)), y: px(Math.min(PH - 14 * k, cyp + R1 - 4 * k)), col: 'var(--cg-ac2)', t: 'memory' + (memSess.length ? ' · session ' + memSess.length : '') } : { x: px(PW - 70 * k), y: px(PH - 74 * k), col: 'var(--cg-ac2)', t: 'memory' });
      // the session graph's own relations (FOLLOWS · RESPONDS · CAUSES · DERIVED …) among what is drawn, and into the
      // prompt's records: the FOLLOWS spine solid, the rest dashed, hidden types folded (the rail's own filter)
      const hide = S.memHide || new Set();
      (S.memEdges || []).forEach((e2) => { const rel = String(e2.relation || e2.type || ''); if (hide.has(rel)) return; const a = out.pos[e2.from_id || e2.from], b = out.pos[e2.to_id || e2.to]; if (!a || !b) return;
        const col = (S.edgeColor && S.edgeColor(rel)) || 'var(--cg-ac2)'; const ec = memEdgeCls(rel); edge(out.cedges, a, b, col, 'mem' + (ec ? ' ' + ec : '') + (a.lit && b.lit ? ' lit' : ''), a.label + ' → ' + b.label + ' · ' + rel.replace(/_/g, ' ').toLowerCase()); });
    }
    } // !quad
    // the loop lane: the track down the left
    if (tracks && loop.length) {
      const MARK = { assess: '◔', verify: '✓', ledger: '▤', clarify: '?', recovery: '↺', gate: '⊘', deliverable: '◆', finalised: '■', journal: '✎', question: '?' };
      const PITCH = 44 * k;
      loop.forEach((s, i) => { const y = LANE_T + 22 * k + i * PITCH; const sub = s.sub != null && s.sub >= 0; lpos.push({ x: LANE_L - 12 * k, y, st: s.status });
        const marks = (s.marks || []).map((m) => ({ g: MARK[m.kind] || '·', kind: m.kind, status: m.status, label: m.label || m.kind }));
        out.loopNodes.push({ i, x: px((sub ? 22 : 10) * k), y: px(y), label: (i + 1) + ' ' + (s.label || s.id), cap: s.cap || '', ms: s.ms || '', marks, branch: s.branch || '',
          cls: stOf(s.status) + (S.lsel === i ? ' sel' : '') + (sub ? ' sub' : '') + (s.pruned ? ' pruned' : '') + (s.branch && !/^sub:/.test(s.branch) ? ' br' : ''),
          title: 'loop step ' + (i + 1) + ' · ' + (s.status || 'pending') + (s.cap ? ' · ' + s.cap : '') + (sub ? ' · a sub-plan step under step ' + (s.sub + 1) : '') + (s.branch && !/^sub:/.test(s.branch) ? ' · branch ' + s.branch + (s.pruned ? ' (pruned)' : '') : '') + (marks.length ? ' · ' + marks.map((m) => m.kind + ' ' + m.status).join(', ') : '') + ' · click to see what it read' });
        if (sub && lpos[s.sub]) out.loopStems.push({ x: px(16 * k), y: px(lpos[s.sub].y + 18 * k), h: px(Math.max(0, y - lpos[s.sub].y - 18 * k)) }); });
      const done = loop.filter((s) => s.status === 'ok').length;
      const runSt = S.run && S.loop && S.loop.length ? S.run.status : '';
      out.regions.push({ x: px(10 * k), y: px(LANE_T - 8 * k < 2 ? 2 : LANE_T - 8 * k), col: runSt === 'fail' ? '#e06c75' : 'var(--cg-ac)', t: (isDag ? 'dag · ' : 'loop · ') + done + ' of ' + loop.length + (runSt === 'fail' ? ' · failed' : runSt === 'ok' ? ' · done' : '') });
      // the track's pill and the plot's step are one node: a hairline from the pill into the plot where both are drawn
      plotLoop.forEach((i) => { if (lpos[i] && spos[i] && view !== 'flow') edge(out.sedges, lpos[i], atP(spos[i]), 'var(--cg-bd2)', 'same', 'step ' + (i + 1) + ' · the lane and the plot'); });
    }
    // the plan row: the track along the top
    if (tracks && plan.length) {
      const pw = (PW - LANE_L - 40 * k) / plan.length;
      plan.forEach((p, i) => { const x = LANE_L + 20 * k + pw * (i + 0.5), st = /^(done|complete|completed|ok)$/.test(p.status || '') ? 'done' : /^(running|active|in_progress)$/.test(p.status || '') ? 'run' : p.status === 'fail' ? 'fail' : 'pend';
        out.planNodes.push({ x: px(x), y: px(18 * k), lx: px(x), ly: px((i % 2 ? 44 : 32) * k), label: p.label || p.id, cls: st, title: 'plan step ' + (i + 1) + ' · ' + (p.label || p.id) + ' · ' + (p.status || '') + (planIsRun && (p.steps || []).length ? ' · ran as loop step' + ((p.steps || []).length > 1 ? 's ' : ' ') + p.steps.map((si) => si + 1).join(', ') : '') });
        // the board's EXECUTED_BY: a plan step to the loop steps that ran it (the running one lit); the goals' row wires only the running goal to the running step
        if (planIsRun) (p.steps || []).forEach((si) => { const b = lpos[si]; if (b) edge(out.sedges, { x, y: 26 * k }, b, st === 'run' ? 'var(--cg-ac)' : 'var(--cg-bd2)', st === 'run' ? 'mem' : 'exec', 'plan step ' + (i + 1) + ' ran as loop step ' + (si + 1)); });
        else if (st === 'run' && loop.length) { const ri = loop.findIndex((s) => s.status === 'running'); const b = lpos[ri >= 0 ? ri : loop.length - 1]; if (b) edge(out.sedges, { x, y: 26 * k }, b, 'var(--cg-ac)', 'mem', 'plan step ' + (i + 1) + ' → the loop step running it'); } });
      out.regions.push({ x: px(LANE_L + 20 * k), y: px(1), col: 'var(--cg-t2)', t: (planIsRun ? 'plan · ' : 'goals · ') + plan.length + (planIsRun ? ' steps · ' : ' · ') + plan.filter((p) => /^(done|complete|completed|ok)$/.test(p.status || '')).length + ' done' });
    }
    // relations INSIDE the graph: what cites what (the context's own edges; a quad's cells drew their own)
    /* AN EDGE IS NO BRIGHTER THAN ITS DIMMEST END. A search dims what does not match rather than removing it,
       but the relations were drawn from the node set alone and took no notice — so searching left full-strength
       edges running to records the search had just faded out, and the graph appeared to be relating things that
       were not there. Measured: searching one of two families drew 2 of 4 nodes and still drew all 3 relations,
       two of them into the dimmed pair. A missed end now dims the edge with it. */
    if (!quad) EDGES.forEach((e2) => { const a = out.pos[e2.from], b = out.pos[e2.to]; if (!a || !b) return; const lit = a.lit && b.lit; const memRel = a.source === 'memory' || b.source === 'memory';
      const miss = QS && !(a.hit && b.hit);
      edge(out.cedges, a, b, miss ? 'var(--cg-bd2)' : memRel ? 'var(--cg-ac2)' : etCol(edgeTypeOf(e2)), (memRel ? 'mem' : lit ? 'rel lit' : 'rel') + (miss ? ' miss' : ''), a.label + ' → ' + b.label + (e2.label ? ' · ' + String(e2.label).replace(/_/g, ' ').toLowerCase() : '')); });
    // All edges on: every record's spoke to the hub — the aide retrieved each for the turn (the old rail graph's edges
    // from the agent), lit when the record is in the prompt; none where the hub is hidden (flow · time)
    // ...and the spokes to the hub answer to the search too: a spoke to a record the search faded out is the
    // same lie as a relation to it, just pointing at the chat instead
    if (!quad && S.allEdges && out.hub && !out.hub.hid) { const hp = { x: out.hub.x, y: out.hub.y }; out.cnodes.concat(out.memNodes.filter((n) => ctxIds.has(n.id))).forEach((n) => { const p = out.pos[n.id]; if (!p) return; const miss = QS && !p.hit;
      edge(out.cedges, hp, p, miss ? 'var(--cg-bd2)' : p.lit ? p.col : 'var(--cg-bd2)', 'spoke' + (p.lit ? ' lit' : '') + (miss ? ' miss' : ''), 'aide → ' + p.label + ' · retrieved for the turn'); }); }
    // the step in focus (or the one running), wired to the records it read — from the fixed lane into the moving plot,
    // and from the plot's own step where the layer draws it
    stepReads.forEach((ids, si) => { const a = lpos[si], s2 = spos[si]; if (!a && !s2) return; const lit = S.lsel != null ? S.lsel === si : (loop[si] && loop[si].status === 'running'); if (!lit && S.lsel != null) return;
      (ids || []).forEach((id) => { const b = out.pos[id]; if (!b) return; if (a) edge(out.sedges, a, atP(b), lit ? 'var(--cg-ac)' : 'var(--cg-bd2)', lit ? 'used' : 'rel', 'step ' + (si + 1) + ' read ' + b.label); if (s2) edge(out.cedges, s2, b, lit ? 'var(--cg-ac)' : 'var(--cg-bd2)', lit ? 'read lit' : 'rel', 'step ' + (si + 1) + ' read ' + b.label); }); });
    // the estate strip along the bottom: the leaves as status boxes grouped under their category (from the snapshot's
    // hub → category → leaf edges), the real links among them, and "ran on" from a loop step to the node its cap ran on
    if (tracks && estate.length) {
      const eById = {}; ((S.estate && S.estate.nodes) || []).forEach((n) => { eById[n.id] = n; });
      const catOf = {}; ((S.estate && S.estate.edges) || []).forEach((ed) => { const a = eById[ed.from], b = eById[ed.to]; if (a && b && a.kind === 'category') catOf[b.id] = a.label; });
      const groups = []; const gIdx = {};
      estate.forEach((n) => { const g = catOf[n.id] || (n.kind === 'service' ? 'Services' : 'Other'); if (gIdx[g] == null) { gIdx[g] = groups.length; groups.push({ name: g, items: [] }); } groups[gIdx[g]].items.push(n); });
      const X0 = LANE_L + 14 * k, X1 = PW - 14 * k, GAP = 18 * k; const per = Math.max(14 * k, Math.min(40 * k, (X1 - X0 - GAP * (groups.length - 1)) / Math.max(1, estate.length)));
      const y = PH + 40 * k; let x = X0;
      const stCls = (s) => s === 'ok' ? 'ok' : s === 'warn' ? 'warn' : s === 'err' || s === 'error' || s === 'fail' ? 'err' : 'unk';
      const estCol = (s) => s === 'ok' ? 'var(--cg-ac2)' : s === 'warn' ? '#e0b060' : s === 'err' || s === 'error' || s === 'fail' ? '#e06c75' : 'var(--cg-t3)';
      groups.forEach((g) => { out.estLabels.push({ x: px(x), y: px(PH + 12 * k), t: g.name + ' · ' + g.items.length });
        g.items.forEach((n) => { const cx = x + per / 2; const r = n.rec || {};
          out.pos[n.id] = { x: cx, y, col: estCol(n.status), source: 'estate', label: n.label, lit: false, rim: 6 * k, ghost: false, score: 0, tok: 0, kind: n.kind || 'node', rec: r, fixed: true };
          out.estNodes.push({ id: n.id, x: px(cx), y: px(y), cls: stCls(n.status) + (S.sel === n.id ? ' on' : ''), label: per >= 34 * k ? String(n.label || '').slice(0, 7) : '', title: (n.label || n.id) + ' · ' + (n.kind || 'node') + ' · ' + (n.status || 'unknown') + (r.detail ? ' · ' + r.detail : '') });
          x += per; });
        x += GAP; });
      out.regions.push({ x: px(X1 - 60 * k), y: px(PH + 12 * k), col: 'var(--cg-est)', t: 'estate' + (lvl.estate === 'focus' ? ' · ran on' : '') });
      // the snapshot's own links among the drawn leaves (a machine serves an instance, a container backs a store)
      ((S.estate && S.estate.edges) || []).forEach((ed) => { const a = out.pos[ed.from], b = out.pos[ed.to]; if (!a || !b || a.source !== 'estate' || b.source !== 'estate') return; edge(out.sedges, a, b, 'var(--cg-bd2)', 'est', a.label + ' → ' + b.label + ' · ' + String(ed.label || 'link').toLowerCase()); });
      // "ran on": a loop step to the node its capability ran on — matched by id or label
      loop.forEach((s, i) => { const a = lpos[i]; if (!a) return; (s.ranOn || []).forEach((name) => { const n = estFind(name); const b = n && out.pos[n.id]; if (!b) return; edge(out.sedges, a, b, 'var(--cg-est)', 'ran', 'step ' + (i + 1) + ' ran on ' + b.label); }); });
    }
    // the record open in the panel: a record, a memory, an estate node — or the loop step in focus
    out.rec = null;
    if (S.sel && out.pos[S.sel]) { const r = out.pos[S.sel]; const by = readBy(S.sel), steps = readBySteps(S.sel);
      const rels = EDGES.filter((e2) => e2.from === S.sel || e2.to === S.sel).map((e2) => { const o = out.pos[e2.from === S.sel ? e2.to : e2.from]; return (o ? o.label : '?') + (e2.label ? ' — ' + String(e2.label).replace(/_/g, ' ').toLowerCase() : ''); }).slice(0, 4);
      const rr = r.rec || {}; const detail = r.source === 'estate' ? [] : [].concat(rr.type || rr.dataset ? [{ k: 'type', v: [rr.type, rr.dataset ? 'dataset ' + rr.dataset : ''].filter(Boolean).join(' · ') }] : [], rr.url ? [{ k: 'url', v: String(rr.url).slice(0, 80), url: String(rr.url) }] : [], (rr.tags || []).length ? [{ k: 'tags', v: rr.tags.slice(0, 8).join(', ') }] : [],
        // a run node (an observed capability run): its status · attempt · progress, and the old graph's evidence links
        r.source === 'run' ? [{ k: 'status', v: rr.status || 'created' }].concat(rr.attempt ? [{ k: 'attempt', v: String(rr.attempt) }] : [], rr.progress != null ? [{ k: 'progress', v: Math.round(Number(rr.progress) * 100) + '%' }] : []) : []);
      const runRoot = r.source === 'run' ? String(rr.parent_run_id || rr.run_id || rr.id || '') : '';
      const links = runRoot ? [{ label: 'Activity evidence ↗', href: '/activity/panel#' + encodeURIComponent('run:' + runRoot) }, { label: 'Memory graph ↗', href: '/memgraph/panel?run_id=' + encodeURIComponent(runRoot) + (rr.session_id ? '&session_id=' + encodeURIComponent(rr.session_id) : '') }] : [];
      out.rec = { id: S.sel, name: r.label, kind: r.source + ' · ' + r.kind, col: r.col, turn: by[0] || null, ghost: r.ghost, family: r.source === 'estate' ? 'estate' : r.source === 'memory' ? 'memory' : 'context', rec: r.rec || null, text: r.source === 'estate' ? '' : textOf(rr).slice(0, 600), url: rr.url || '', links,
        rows: r.source === 'estate' ? [{ k: 'kind', v: r.kind }, { k: 'status', v: (r.rec && r.rec.status) || 'unknown' }, { k: 'detail', v: (r.rec && r.rec.detail) || '—' }, { k: 'temperature', v: r.rec && r.rec.temp_c != null ? r.rec.temp_c + ' °C' : '—' }] : r.sess ? [{ k: 'kind', v: r.kind + (r.rec && r.rec.source_type ? ' · ' + r.rec.source_type : '') }, { k: 'recalled', v: r.ghost ? 'in the session, never injected' : 'injected' + (by.length ? ' · ' + by.join(', ') : '') }, { k: 'created', v: String((r.rec && r.rec.created_at) || '').replace('T', ' ').slice(0, 16) || '—' }, { k: 'importance', v: r.score.toFixed(2) }]
          : [{ k: 'relevance', v: r.score.toFixed(2) + (r.ghost ? ' · related, not injected' : r.lit ? ' · in this prompt' : by.length ? ' · in the prompt of ' + by.join(', ') : ' · not read') }, { k: 'tokens', v: String(r.tok) }, { k: 'read by', v: by.length ? by.join(' · ') : '—' }, { k: 'loop steps', v: steps.length ? steps.map((s) => 'step ' + s).join(' · ') : '—' }, { k: 'source', v: r.source + ' · ' + r.kind }].concat(detail), rels }; }
    else if (S.lsel != null && loop[S.lsel]) { const s = loop[S.lsel]; const rd = (stepReads[S.lsel] || []).map((id) => out.pos[id]).filter(Boolean);
      const pl = planIsRun ? plan.findIndex((p) => (p.steps || []).indexOf(S.lsel) >= 0) : -1;
      out.rec = { id: s.id, name: (S.lsel + 1) + ' · ' + (s.label || s.id), kind: (isDag ? 'dag' : 'loop') + ' · step', col: 'var(--cg-ac)', turn: null, ghost: false, family: 'loop', rec: s,
        rows: [{ k: 'status', v: s.status || 'pending' }, { k: 'capability', v: s.cap || '—' }, { k: 'took', v: s.ms || '—' }, { k: 'read', v: rd.length ? rd.length + ' record' + (rd.length > 1 ? 's' : '') : '—' }].concat(pl >= 0 ? [{ k: 'plan step', v: (pl + 1) + ' · ' + (plan[pl].label || '') }] : []).concat(s.ranOn && s.ranOn.length ? [{ k: 'ran on', v: s.ranOn.join(' · ') }] : []),
        rels: rd.slice(0, 4).map((r) => 'read ' + r.label).concat((s.marks || []).slice(0, 3).map((m) => m.kind + ' · ' + m.status)) }; }
    // All edges off: only the relations that touch the prompt (lit, used, memory, the chain) stay; the dim ones fold away
    if (!S.allEdges) { out.cedges = out.cedges.filter((e2) => e2.cls !== 'rel'); out.sedges = out.sedges.filter((e2) => e2.cls !== 'rel'); }
    // each source with its records, its TOKENS and its level — the meter is sized by tokens, so it is the budget
    // and the legend at once rather than a second row of chips saying the same names
    out.srcs = srcs.map((s) => { const own = ctx.filter((n) => (n.source || '?') === s);
      return { name: s, col: color(s), n: own.length, level: srcLvl(s), tok: own.reduce((a, n) => a + tokOf(n), 0) }; });
    // a source switched OFF still has to be reachable, or it could never be switched back on — it keeps its
    // place on the meter, at the width its records would have had
    out.offSrcs = [...new Set(nodes.filter((n) => n.source !== 'memory').map((n) => n.source || '?'))].filter((s) => srcLvl(s) === 'off').map((s) => { const own = nodes.filter((n) => (n.source || '?') === s);
      return { name: s, col: color(s), n: own.length, level: 'off', tok: own.reduce((a, n) => a + tokOf(n), 0) }; });
    // the other families' chips: the mixer — each carries its level (off · focus · all) and its count
    out.families = [];
    const nMem = memCtxAll.length + (S.memory || []).filter((m) => m && m.id && !ctxIds.has(m.id)).length;
    if (nMem) out.families.push({ name: 'memory', fam: 'memory', col: color('memory'), n: nMem, on: lvl.memory !== 'off', level: lvl.memory });
    if ((S.dag || []).length && !(S.loop && S.loop.length)) out.families.push({ name: 'dag', fam: 'loop', col: 'var(--cg-ac)', n: S.dag.length, on: lvl.loop !== 'off', level: lvl.loop });
    if (S.loop && S.loop.length) out.families.push({ name: 'loop', fam: 'loop', col: 'var(--cg-ac)', n: S.loop.length, on: lvl.loop !== 'off', level: lvl.loop });
    if (runPlan.length || (S.plan || []).length) out.families.push({ name: 'plan', fam: 'plan', col: 'var(--cg-t2)', n: runPlan.length || S.plan.length, on: lvl.plan !== 'off', level: lvl.plan });
    if (estAll.length) out.families.push({ name: 'estate', fam: 'estate', col: 'var(--cg-est)', n: estAll.length, on: lvl.estate !== 'off', level: lvl.estate });
    out.ghosts = nodes.filter((n) => n.included === false).length + memSess.filter((n) => !n._injected).length;
    // the LIST: the records as rows (the old renderCtxList's: source dot · label · relevance bar · tokens · included),
    // the most relevant first, the search's hits only while a search is on; every row knows its record for the panel
    out.list = ctx.concat(mem).filter((n) => matchQ(n)).map((n) => { const p = out.pos[n.id] || {}; const sc = Math.max(0, Math.min(1, n.score == null ? 0.5 : +n.score));
      /* `text` stayed at 140 characters because the row only ever showed a one-line tease. The list under the
         graph is the place the whole record is meant to be readable, so the row carries the body (capped, with
         the height held by CSS rather than by cutting the words off) and the facts that were only ever in the
         record card: which side of the turn read it, what kind of thing it is, and where it came from. */
      return { id: n.id, label: n.label || n.id, source: n.source || '?', col: p.col || color(n.source), score: sc, tok: tokOf(n), included: n._sess ? !!p.lit : n.included !== false, sess: !!n._sess, lit: !!p.lit, sel: S.sel === n.id, text: textOf(n).replace(/\s+/g, ' ').slice(0, 140), url: n.url || '', kind: p.icon || kindOf(n, n.source || '?'),
        full: textOf(n).replace(/[ \t]+/g, ' ').trim().slice(0, 1400), by: p.by === 'a' ? 'a' : 'u',
        type: String(n.type || '') || '', turn: p.turn || '' }; })
      .sort((a, b) => (a.sess === b.sess ? b.score - a.score : a.sess ? 1 : -1));
    out.listTotal = ctx.length + mem.length;
    /* the relations as PAIRS, not geometry: out.cedges carries them already but positioned for this plot, and a
       layout that places its marks differently (the mini galaxy) needs the pairs to join its own (defect 60) */
    out.rels = EDGES.map((e0) => ({ from: String(e0.from_id || e0.from || ''), to: String(e0.to_id || e0.to || ''), type: edgeTypeOf(e0) }))
      .filter((r) => r.from && r.to && r.from !== r.to);
    // the edge types drawn (and the folded ones), with counts — the chip row's second group
    const etc = {}; edgesAll.forEach((e0) => { const t = edgeTypeOf(e0); etc[t] = (etc[t] || 0) + 1; });
    out.edgeTypes = Object.keys(etc).sort((a, b) => etc[b] - etc[a]).map((t) => ({ name: t, n: etc[t], on: !eOff.has(t), col: etCol(t) }));
    // the frames scrubber: the turns' snapshots, the one in view marked; "live" is the current set
    out.frames = frames.filter((f) => f && f.id != null).map((f) => ({ id: f.id, label: f.label || String(f.id), ts: f.ts || '', n: (f.nodes || []).length, on: frame != null && String(f.id) === String(frame.id) }));
    out.frame = frame ? { id: frame.id, label: frame.label || String(frame.id) } : null;
    out.lanes = { l: LANE_L, t: LANE_T, b: LANE_B, hasLanes };
    out.mix = lvl;
    return out;
  }
  // the mini: the same layout in a small box — the tracks scaled to it (the `context_graph` widget form's M face)
  function mini(S, w, h) { const v = (S && S.view) || 'galaxy'; return compute(Object.assign({}, S || {}, { mini: true, view: v === 'quad' ? 'galaxy' : v, pan: null }), w || 262, h || 196); }
  // a state from a widget record's data ({nodes, rels|edges, focus, memory, memEdges, loop, plan, estate, view, mix})
  function stateFrom(d) { d = d || {}; const edges = (d.edges || d.rels || d.links || []).map((e) => ({ from: e.from != null ? e.from : e.source, to: e.to != null ? e.to : e.target, label: e.label || e.kind || e.type || '' }));
    return { view: d.view || 'galaxy', nodes: d.nodes || [], edges, focus: d.focus || (d.nodes || []).filter((n) => n && n.included !== false).map((n) => n.id), reads: d.reads || {}, stepReads: d.stepReads || [], loop: d.loop || [], runPlan: d.runPlan || [], run: d.run || null, dag: d.dag || [], dagEdges: d.dagEdges || [], plan: d.plan || [], estate: d.estate || { nodes: [], edges: [] }, memory: d.memory || [], memEdges: d.memEdges || [], mix: d.mix || {}, layersOff: new Set(d.off || []), related: d.related !== false, allEdges: !!d.allEdges, sel: d.sel || null, lsel: d.lsel == null ? null : d.lsel, pan: { x: 0, y: 0, z: 1 }, color: d.color || null, memColor: d.memColor || null, edgeColor: d.edgeColor || null, turn: d.turn || '',
      q: d.q || '', list: !!d.list, frames: d.frames || [], frame: d.frame == null ? null : d.frame, edgesOff: new Set(d.edgesOff || []) }; }

  // the pan/zoom that puts a record (its layout position) at the plot's centre at zoom z — the inverse of compute()'s atP
  function panTo(p, PW, PH, z) { z = z || 1; return { z, x: -(p.x - PW / 2) * z, y: -(p.y - PH / 2) * z }; }
  /* ── the drawing, shared by the element and the mini: the layout → markup in the element's own classes ── */
  const stAt = (x, y) => 'left:' + x + 'px;top:' + y + 'px;';
  const edgeHtml = (e) => '<div class="cg-edge ' + e.cls + '" title="' + esc(e.title) + '" style="' + stAt(e.x, e.y) + 'width:' + e.len + 'px;background:' + esc(e.col) + ';transform:rotate(' + e.deg + 'deg)"></div>';
  // what pans and zooms: rings, spokes, the plate and its discs, stems and pins, the records, the memory ring, the plot's
  // own steps and plan diamonds, the relations inside, the region captions, the hub
  function drawPlot(o) {
    let h = '';
    o.rings.forEach((r) => { h += '<div class="cg-ring' + (r.cls ? ' ' + r.cls : '') + '" style="' + stAt(r.cx, r.cy) + 'width:' + r.d + 'px;height:' + r.d + 'px' + (r.tf ? ';--tf:' + r.tf : '') + '"></div>'; });
    o.spokes.forEach((s) => { h += '<div class="cg-spoke" style="' + stAt(s.x, s.y) + 'width:' + s.len + 'px;transform:rotate(' + s.deg + 'deg)"></div>'; });
    if (o.plate) h += '<div class="cg-plate" style="' + stAt(o.plate.x, o.plate.y) + 'width:' + o.plate.w + 'px;height:' + o.plate.h + 'px"></div>';
    (o.discs || []).forEach((d) => { h += '<div class="cg-disc" data-fam="' + esc(d.name) + '" style="' + stAt(d.x, d.y) + 'width:' + d.d + 'px;height:' + d.d + 'px;--c:' + esc(d.col) + ';--a0:' + d.a0 + ';--a1:' + d.a1 + ';--m0:' + d.m0 + ';--m1:' + d.m1 + ';transform:' + d.tf + '"></div>'; });
    (o.dividers || []).forEach((d) => { h += '<div class="cg-div" style="' + stAt(d.x, d.y) + 'width:' + d.w + 'px;height:' + d.h + 'px"></div>'; });
    o.stems.forEach((s) => { h += '<div class="cg-stem" style="' + stAt(s.x, s.y) + 'height:' + s.h + 'px"></div>'; });
    (o.pins || []).forEach((p) => { h += '<div class="cg-pin' + (p.cls ? ' ' + p.cls : '') + '" style="' + stAt(p.x, p.y) + 'height:' + p.h + 'px;--c:' + esc(p.col) + '"></div>'; });
    o.sectorLabels.forEach((s) => { h += '<div class="cg-slbl' + (s.lane ? ' lane' : '') + '" style="' + stAt(s.x, s.y) + 'color:' + esc(s.col) + '">' + esc(s.name) + '</div>'; });
    o.cedges.forEach((e) => { h += edgeHtml(e); });
    o.cnodes.forEach((n) => { h += '<div class="cg-node ' + n.cls + '" data-id="' + esc(n.id) + '" data-kind="' + esc(n.kind || '') + '" style="' + stAt(n.x, n.y) + 'width:' + n.d + 'px;height:' + n.d + 'px;--nc:' + esc(n.col) + ';opacity:' + n.op + '">' + (n.d >= 14 ? iconSvg(n.kind) : '') + (n.d >= 30 ? '<span>' + esc(String(n.id).replace(/^__\w+__/, '').slice(0, 6)) + '</span>' : '') + '</div>'; });
    o.memNodes.forEach((n) => { h += '<div class="cg-mem ' + n.cls + '" data-id="' + esc(n.id) + '" data-kind="' + esc(n.kind || '') + '" style="' + stAt(n.x, n.y) + (n.col ? ';--mc:' + esc(n.col) : '') + (n.op && n.op !== '1' ? ';--op:' + n.op : '') + '"></div>'; });
    (o.memLabels || []).forEach((l) => { h += '<div class="cg-memg" style="' + stAt(l.x, l.y) + 'color:' + esc(l.col) + '">' + esc(l.t) + '</div>'; });
    (o.stepNodes || []).forEach((n) => { h += '<div class="cg-step ' + n.cls + '" data-i="' + n.i + '" data-id="' + esc(n.id) + '" title="' + esc(n.title) + '" style="' + stAt(n.x, n.y) + '">' + iconSvg('step') + '</div><div class="cg-stepl" style="' + stAt(n.x, n.y + 9 * (o.k || 1)) + '">' + esc(n.label) + '</div>'; });
    (o.planPlot || []).forEach((n) => { h += '<div class="cg-pnode ' + n.cls + '" title="' + esc(n.title) + '" style="' + stAt(n.x, n.y) + '"></div><div class="cg-stepl" style="' + stAt(n.x, n.y + 8 * (o.k || 1)) + '">' + esc(n.label) + '</div>'; });
    o.regions.forEach((r) => { h += '<div class="cg-region' + (r.cell ? ' cell' : '') + '" style="' + stAt(r.x, r.y) + 'color:' + esc(r.col) + '">' + esc(r.t) + '</div>'; });
    const tok = o.tokens >= 1000 ? (o.tokens / 1000).toFixed(1) + 'k' : o.tokens;
    if (o.hubs) o.hubs.forEach((hb) => { h += '<div class="cg-hub cell' + (hb.hid ? ' hid' : '') + '" style="' + stAt(hb.x, hb.y) + '"><b>aide</b><span>' + tok + '</span></div>'; });
    else h += '<div class="cg-hub' + (o.hub.hid ? ' hid' : '') + '" style="' + stAt(o.hub.x, o.hub.y) + '"><b>aide</b><span>' + tok + '</span></div>';
    // the turn under the hub — the frame in view (the transcript's scroll), or the host's turn label for the live set
    if (o.turn && !o.hub.hid && !o.hubs) h += '<div class="cg-hubt" style="' + stAt(o.hub.x, o.hub.y + 34 * (o.k || 1)) + '">' + esc(o.turn) + '</div>';
    return h;
  }
  // what stays put: the tracks (the loop lane, the plan row, the estate strip), their relations into the plot, the record
  function drawLanes(o, opts) {
    opts = opts || {}; let l = '';
    o.sedges.forEach((e) => { l += edgeHtml(e); });
    o.planNodes.forEach((n) => { l += '<div class="cg-plan ' + n.cls + '" title="' + esc(n.title) + '" style="' + stAt(n.x, n.y) + '"></div><div class="cg-planl ' + n.cls + '" style="' + stAt(n.lx, n.ly) + '">' + esc(n.label) + '</div>'; });
    o.estLabels.forEach((g) => { l += '<div class="cg-estg" style="' + stAt(g.x, g.y) + '">' + esc(g.t) + '</div>'; });
    o.estNodes.forEach((n) => { l += '<div class="cg-est ' + n.cls + '" data-id="' + esc(n.id) + '" title="' + esc(n.title) + '" style="' + stAt(n.x, n.y) + '"></div>' + (n.label ? '<div class="cg-estl" style="' + stAt(n.x, n.y + 9) + '">' + esc(n.label) + '</div>' : ''); });
    o.loopStems.forEach((s) => { l += '<div class="cg-lstem" style="' + stAt(s.x, s.y) + 'height:' + s.h + 'px"></div>'; });
    o.loopNodes.forEach((n) => { l += '<div class="cg-loop ' + n.cls + '" data-i="' + n.i + '" title="' + esc(n.title) + '" style="' + stAt(n.x, n.y) + '"><b>' + esc(n.label.slice(0, 16)) + '</b><span class="c">' + esc(n.cap) + '</span><span class="t">' + esc(n.ms) + '</span>'
      + (n.marks && n.marks.length ? '<span class="m">' + n.marks.map((m) => '<i class="' + esc(m.status) + ' ' + esc(m.kind) + '" title="' + esc(m.kind + ' · ' + m.status + ' · ' + m.label) + '">' + esc(m.g) + '</i>').join('') + '</span>' : '') + '</div>'; });
    if (o.rec && !opts.noRecord) l += recordCard(o.rec, { bottom: o.lanes.b ? o.lanes.b + 6 : 0, zoom: true });
    return l;
  }
  // the record card — the panel in the element, the overlay in the mini: the record's name and kind, its rows (relevance ·
  // tokens · read by · loop steps · source · type · url · tags), its TEXT, its relations, the actions
  function recordCard(r, opts) {
    opts = opts || {};
    const rows = opts.compact ? r.rows.slice(0, 3) : r.rows;
    return '<div class="cg-rec' + (opts.compact ? ' compact' : '') + '" data-id="' + esc(r.id) + '" style="--gc:' + esc(r.col) + (opts.bottom ? ';bottom:' + opts.bottom + 'px' : '') + '"><span class="cg-rec-h"><i class="' + (r.ghost ? 'ghost' : '') + '"></i><b>' + esc(r.name) + '</b><span class="mono">' + esc(r.kind) + '</span><span class="x" data-a="close">✕</span></span>'
      + rows.map((x) => '<span class="cg-rec-r"><span class="k">' + esc(x.k) + '</span>' + (x.url ? '<a class="v" href="' + esc(x.url) + '" target="_blank" rel="noopener">' + esc(x.v) + '</a>' : '<span class="v">' + esc(x.v) + '</span>') + '</span>').join('')
      + (r.text ? '<div class="cg-rec-t">' + esc(opts.compact ? r.text.slice(0, 160) : r.text) + '</div>' : '')
      + (opts.compact ? '' : (r.rels || []).map((x) => '<span class="cg-rec-l"><i></i>' + esc(x) + '</span>').join(''))
      + '<span class="cg-rec-a">' + (r.turn ? '<button class="pri" data-a="turn" data-mid="' + esc(r.turn) + '">Focus turn</button>' : '') + (r.family === 'estate' || r.family === 'loop' ? '' : '<button data-a="toggle" data-id="' + esc(r.id) + '">' + (r.ghost ? (opts.compact ? 'Include' : 'Include in the prompt') : (opts.compact ? 'Exclude' : 'Exclude from the prompt')) + '</button><button data-a="open" data-id="' + esc(r.id) + '">Open</button>')
      + (opts.zoom && r.family !== 'loop' ? '<button data-a="zoom" data-id="' + esc(r.id) + '" title="Pan the plot to this record">Zoom to</button>' : '')
      + (r.url ? '<button data-a="preview" data-id="' + esc(r.id) + '" data-url="' + esc(r.url) + '" title="Open the page in the browser pane">' + (opts.compact ? 'Preview' : 'Preview page') + '</button>' : '')
      + (r.links || []).map((l) => '<a class="lnk" href="' + esc(l.href) + '" target="_blank" rel="noopener">' + esc(l.label) + '</a>').join('') + '</span></div>';
  }
  // the LIST drawer: the rows from compute() — a row is the record (click → the panel), its dot, label, relevance bar,
  // tokens and the include / exclude mark (data-a="toggle"); the head says how many, and how many the search hit
  function listHtml(o, opts) {
    opts = opts || {};
    const rows = opts.limit ? o.list.slice(0, opts.limit) : o.list;
    const head = (o.q ? rows.length + ' of ' + o.listTotal + ' match “' + o.q + '”' : o.listTotal + ' record' + (o.listTotal === 1 ? '' : 's')) + (o.frame ? ' · frame ' + o.frame.label : '');
    const anyPrompt = o.list.some((r) => !r.sess);
    return '<div class="cg-list' + (opts.compact ? ' compact' : '') + '"><div class="cg-list-h"><span>' + esc(head) + '</span>' + (anyPrompt ? '<button class="all" data-a="incl-all" title="Include every record in the prompt">' + (opts.compact ? 'incl' : 'Incl all') + '</button><button class="all" data-a="excl-all" title="Exclude every record from the prompt">' + (opts.compact ? 'excl' : 'Excl all') + '</button>' : '') + (opts.closable === false ? '' : '<span class="x" data-a="list" title="Close the list">✕</span>') + '</div><div class="cg-list-b">'
      + (rows.length ? rows.map((r) => '<div class="cg-row' + (r.included ? '' : ' excl') + (r.lit ? ' lit' : '') + (r.sel ? ' on' : '') + '" data-id="' + esc(r.id) + '" title="' + esc(r.label + ' · ' + r.source + ' · ' + (r.sess ? 'importance ' : 'relevance ') + r.score.toFixed(2) + ' · ' + r.tok + ' tokens' + (r.sess ? (r.included ? ' · in the prompt' : ' · in the session, not injected') : r.included ? '' : ' · excluded from the prompt')) + '" style="--rc:' + esc(r.col) + '">'
        + '<i class="dot"></i><b>' + esc(r.label) + '</b><span class="bar"><i style="width:' + Math.round(r.score * 100) + '%"></i></span><span class="tok">' + r.tok + '</span>'
        + (r.url ? '<button class="pv" data-a="preview" data-id="' + esc(r.id) + '" data-url="' + esc(r.url) + '" title="Open the page in the browser pane">◫</button>' : '<span class="pv"></span>')
        + (r.sess ? '<span></span>' : '<button data-a="toggle" data-id="' + esc(r.id) + '" title="' + (r.included ? 'Exclude from the prompt' : 'Include in the prompt') + '">' + (r.included ? '✕' : '＋') + '</button>')
        /* FULL DETAIL, not a one-line tease. The row showed 140 characters and nothing else, so the only way to
           learn what a record actually was, or which side of the turn read it, was to click through to the card.
           Under the graph that is the wrong way round: the list is where the records are read. The compact face
           (the rail's mini) keeps the tease — it has a box the size of a postcard. */
        + (opts.compact
            ? (r.text ? '<small>' + esc(r.text) + '</small>' : '')
            : '<div class="cg-row-d">'
              + '<div class="cg-row-m"><span class="fam">' + esc(r.source) + '</span>'
              + (r.type && r.type !== r.source ? '<span>' + esc(r.type) + '</span>' : '')
              + '<span>' + (r.sess ? 'importance ' : 'relevance ') + r.score.toFixed(2) + '</span>'
              + '<span>' + r.tok + ' tok</span>'
              + '<span class="' + (r.included ? 'in' : 'out') + '">'
              + (r.sess ? (r.included ? 'in the prompt' : 'in the session, not injected')
                        : r.included ? (r.by === 'a' ? 'read by the answer' : 'in the prompt') : 'excluded')
              + '</span>' + (r.turn ? '<span>' + esc(r.turn) + '</span>' : '') + '</div>'
              + (r.full ? '<div class="cg-row-x">' + esc(r.full) + '</div>' : '')
              + (r.url ? '<a class="cg-row-u" href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.url.slice(0, 90)) + '</a>' : '')
              + '</div>')
        + '</div>').join('') : '<div class="cg-list-e">' + (o.q ? 'nothing matches' : 'no records yet') + '</div>')
      + '</div></div>';
  }
  // the mini's face: the whole layout in one box (no chips, no header) — the widget form calls this with the state
  /* The mini has two faces (Notes/42 defect 60, redone against the rendered board). DETAILED is what it grew into on this edge: every family on the
     plot - the memory ring, the loop, the plan, the estate - with the lanes beneath it, the record picked showing its
     card and the list drawer. SIMPLE is the board's: the context plot by itself, its rings, its spokes, its records
     and the hub, and nothing else on top of it. The element draws whichever face it is handed; the switch is the
     host's, so the quick menu and any other host can remember it their own way. */
  function miniHtml(S, w, h, opts) {
    w = w || 262; h = h || 196; opts = opts || {}; S = S || {};
    const simple = String(opts.style || S.miniStyle || 'detailed').toLowerCase() === 'simple';
    const detail = simple ? null : (opts.detail !== undefined ? opts.detail : S.sel), list = simple ? false : (opts.list !== undefined ? !!opts.list : !!S.list), q = simple ? '' : (opts.q !== undefined ? opts.q : S.q);
    /* The simple face is the board's plot with EVERY source on it - the board's legend names eight and its plot
       draws eight, Memory recalls among them. It was built by folding the other families away, which took the one
       family the user had asked about by name off the face that was meant to be the board's. What simple drops is
       the lanes beneath the plot, the picked record's card and the drawer: the graph itself is whole. */
    /* The simple face is the design's GALAXY: the context records and the memory recalls around the hub, and
       nothing laid over them. The loop, the plan and the estate are folded away - the board's galaxy has no lane,
       no plan row and no estate strip - but the RECALLS are not: the board names "Memory recalls" in the legend
       and in the list beside the other seven sources, and the memory level governs the recalls as well as the arm
       (defect 65), so folding it took them out of the plot too. The memories are on every graph. */
    const base = simple ? Object.assign({}, S, { mix: Object.assign({}, S.mix || {}, { loop: 'off', plan: 'off', estate: 'off' }), list: false }) : S;
    const o = mini(Object.assign({}, base, { sel: detail || null, q: q || '' }), w, h);
    if (simple) return '<div class="cg-mini simple" style="width:' + w + 'px;height:' + h + 'px"><div class="cg-in">' + drawMiniGalaxy(o, w, h) + '</div></div>';
    return '<div class="cg-mini' + (list ? ' listing' : '') + '" style="width:' + w + 'px;height:' + h + 'px"><div class="cg-in">' + drawPlot(o) + '</div><div class="cg-lanes">' + drawLanes(o, { noRecord: true }) + '</div>'
      /* opts.card === false marks the picked node WITHOUT laying its card over the plot. The rail's menu is
         262px wide, so the card covered the graph the record was found on; the chat opens the record in the
         list beneath instead, and still wants the node marked so the two agree on what is picked. */
      + (list ? listHtml(o, { compact: true, limit: 40 }) : '') + (o.rec && opts.card !== false ? recordCard(o.rec, { compact: true }) : '') + '</div>';
  }
  /* THE DESIGN'S MINI GALAXY (Canvas.dc.html 6164-6199, .gal/.gd/.gring/.glb/.ghub). A different drawing from
     drawPlot, not a setting on it: every source contributes its most relevant THREE OR FOUR records and no more,
     each source takes its own sector of the circle, relevance falls outward along 28/50/72/94, the outermost mark
     of a source is hollow and carries that source's name. Thirty-odd marks in all - which is what makes it read as
     a galaxy rather than the crammed crescents you get from drawing all 412 ontologies (Notes/42 defect 60). */
  const GAL_LABEL = { agent: 'Agent + system', skill: 'Skills', ontology: 'Ontologies', memory: 'Memory recalls',
    vector: 'Vector matches', graph: 'Graph (Neo4j)', fabric: 'Fabric records', cap: 'Capabilities', web: 'Web',
    news: 'News', run: 'Runs', related_qa: 'Related Q&A', worldview: 'Worldview', entities: 'Entities', urls: 'Links', both: 'Memory + vector' };
  function drawMiniGalaxy(o, w, h) {
    const W = w || 262, H = h || 196, cx = W / 2, cy = H / 2 + 6, RAD = Math.PI / 180, RIM = 82;
    // the view drives the arrangement, exactly as the board's does (Canvas.dc.html 6164-6199)
    const GV = String(o.view || 'galaxy') === 'time' ? 'timeline' : String(o.view || 'galaxy');
    const isoP = (x, y, z) => { const A = 32 * RAD, dx = x - cx, dy = y - cy;
      return [cx + dx * Math.cos(A) - dy * Math.sin(A), cy + (dx * Math.sin(A) + dy * Math.cos(A)) * 0.56 - z]; };
    const by = {}; const seen = [];
    (o.list || []).forEach((r) => { const k = String(r.source || '?'); if (!by[k]) { by[k] = []; seen.push(k); } by[k].push(r); });
    seen.forEach((k) => by[k].sort((a, b) => (b.score || 0) - (a.score || 0)));
    // the element names a sector by its raw key ("ontology", "cap"); the board names it as the legend does
    const order = seen.slice().sort((a, b) => ((ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99)));
    const SRC = order.map((k) => ({ key: k, name: GAL_LABEL[k] || k, col: by[k][0] && by[k][0].col, rows: by[k] }));
    if (!SRC.length) return '<div class="cg-gal"></div>';
    const P = {}, dots = [], labels = [], stems = [], at = {}, lead = {};
    SRC.forEach((s, si) => {
      const N = Math.min(3 + (si % 2), s.rows.length);
      const a0 = -90 + si * (360 / SRC.length);
      for (let k = 0; k < N; k++) {
        const row = s.rows[k], rel = row.score != null ? Math.max(0, Math.min(1, +row.score)) : (0.92 - k * 0.17);
        let x, y;
        if (GV === 'flow') { x = 18 + si * 31; y = 30 + k * 26; }
        else if (GV === 'timeline') { x = 30 + k * 60 + (si % 3) * 14; y = 16 + si * 22; }
        else {
          const r = 28 + k * 22, a = (a0 + (k - (N - 1) / 2) * 11) * RAD;
          x = cx + Math.cos(a) * r; y = cy + Math.sin(a) * r;
          if (GV === 'iso') {
            // the same mark, turned onto the plane and raised by its relevance, with a pin down to the floor
            const tw = (r / 80) * 40 * RAD, a2 = a + tw, gx = cx + Math.cos(a2) * r, gy = cy + Math.sin(a2) * r, z = rel * 22;
            const q0 = isoP(gx, gy, 0), q = isoP(gx, gy, z); x = q[0]; y = q[1];
            if (z > 4) stems.push('<span class="cg-gstem" style="left:' + q0[0].toFixed(1) + 'px;top:' + (q0[1] - z).toFixed(1) + 'px;height:' + z.toFixed(1) + 'px"></span>');
          }
        }
        P[si + ':' + k] = [x, y]; at[row.id] = { p: [x, y], col: s.col };
        if (k === 0) lead[s.key] = at[row.id];   // the source's leading mark stands for its records that are not drawn
        const cls = (rel > 0.8 && row.included !== false ? 'lit ' : '') + (k === N - 1 ? 'hollow' : '');
        dots.push('<span class="cg-gd ' + cls + '" data-id="' + esc(row.id) + '" style="left:' + x.toFixed(1) + 'px;top:' + y.toFixed(1) + 'px;--c:' + esc(s.col || 'var(--cg-t2)') + ';opacity:' + (0.45 + rel * 0.55).toFixed(2) + '"></span>');
      }
      /* the name rings the OUTSIDE, at the rim, along this source's own sector - not at its outermost mark. The
         board places it at the mark because its data is a mock where every source has three or four; a source with
         a single record has that mark at r=28, and its name would be drawn across the hub. */
      // the name sits where that view puts the source: at the rim in galaxy and iso, by its column or its row otherwise
      let lx, ly;
      if (GV === 'flow') { lx = 18 + si * 31 - 6; ly = 30 + (N - 1) * 26 + 16; }
      else if (GV === 'timeline') { lx = 6; ly = 16 + si * 22; }
      else { const ar = a0 * RAD; const rr = GV === 'iso' ? RIM * 0.88 : RIM;
        const q = GV === 'iso' ? isoP(cx + Math.cos(ar) * rr, cy + Math.sin(ar) * rr, 10) : [cx + Math.cos(ar) * rr, cy + Math.sin(ar) * rr];
        lx = q[0]; ly = q[1]; }
      const lft = GV === 'timeline' ? false : lx < cx;
      labels.push('<span class="cg-glb' + (lft ? ' l' : '') + '" style="left:' + lx.toFixed(1) + 'px;top:' + (ly - 4).toFixed(1) + 'px;color:' + esc(s.col || 'var(--cg-t2)') + '">' + esc(s.name.replace(' + system', '').replace(' matches', '').replace(' recalls', '')) + '</span>');
    });
    // the rings belong to the round views; flow and time have no circle to sit on
    const rc = GV === 'iso' ? isoP(cx, cy, 0) : [cx, cy];
    const rings = (GV === 'galaxy' || GV === 'iso') ? [28, 50, 72].map((r) => '<span class="cg-gring" style="left:' + rc[0].toFixed(1) + 'px;top:' + rc[1].toFixed(1) + 'px;width:' + (r * 2) + 'px;height:' + (r * 2) + 'px"></span>').join('') : '';
    // the spokes: every mark to the hub when All edges is on, else the relations the state actually carries
    const seg = (a, b, col, cls) => { const dx = b[0] - a[0], dy = b[1] - a[1];
      return '<span class="cg-gedge ' + cls + '" style="left:' + a[0].toFixed(1) + 'px;top:' + a[1].toFixed(1) + 'px;width:' + Math.hypot(dx, dy).toFixed(1) + 'px;transform:rotate(' + (Math.atan2(dy, dx) * 180 / Math.PI).toFixed(1) + 'deg);--c:' + esc(col) + '"></span>'; };
    /* a connector that has to stand apart from its neighbours is bowed; one that does not is the board's straight
       span. The bow is a quadratic through a control point pushed off the midpoint's normal. */
    const bowed = (a, b, bow, col, cls) => {
      if (!bow) return seg(a, b, col, cls);
      const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2, dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy) || 1;
      const qx = (mx - dy / L * bow).toFixed(1), qy = (my + dx / L * bow).toFixed(1);
      return '<svg class="cg-gcur ' + cls + '" style="--c:' + esc(col) + '" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none"><path d="M' + a[0].toFixed(1) + ' ' + a[1].toFixed(1) + ' Q' + qx + ' ' + qy + ' ' + b[0].toFixed(1) + ' ' + b[1].toFixed(1) + '"/></svg>';
    };
    let edges = '';
    /* THE INTER CONNECTORS: the relations the state holds, between marks this face actually drew. A pair joined
       more than once, and a record fanning out to several others, would lie along the same line and read as one
       thick edge - so each is bowed, alternating in sign and growing with the count on that pair. A lone relation
       bows by nothing and is straight, as the board's are. */
    /* This face draws a few marks per source, so a relation's own two records are usually not both among them.
       A relation still SAYS something at this size - that these two sources are tied - so one whose record is not
       drawn is carried by that source's leading mark. It is a summary, like the rest of this face. */
    const srcOf = {}; (o.list || []).forEach((r) => { srcOf[r.id] = String(r.source || '?'); });
    const markOf = (id) => at[id] || lead[srcOf[id]] || null;
    const pairN = {}; let drawn = 0;
    (o.rels || []).forEach((r) => {
      if (drawn >= 14) return;                       // a handful, as the board has - not a hairball
      const a = markOf(r.from), b = markOf(r.to); if (!a || !b || a === b) return;
      const ka = a.p.join(), kb = b.p.join(); if (ka === kb) return;
      const key = ka < kb ? ka + '|' + kb : kb + '|' + ka;
      const n = (pairN[key] = (pairN[key] || 0) + 1);
      if (n > 3) return;                             // three between the same two marks is plenty
      const bow = n === 1 ? 0 : (n % 2 ? 1 : -1) * Math.ceil((n - 1) / 2) * 7;
      edges += bowed(a.p, b.p, bow, a.col || 'var(--cg-t2)', 'rel' + (r.type === 'mem' ? ' mem' : ''));
      drawn++;
    });
    const hubAt = GV === 'flow' ? [cx, H - 12] : GV === 'timeline' ? [12, H - 10] : GV === 'iso' ? isoP(cx, cy, 16) : [cx, cy];
    if (o.allEdges) SRC.forEach((s, si) => { for (let k = 0; k < 4; k++) { const p = P[si + ':' + k]; if (p) edges += seg(p, hubAt, s.col || 'var(--cg-t2)', 'faint'); } });
    const tk = o.tokens >= 1000 ? (o.tokens / 1000).toFixed(1) + 'k' : (o.tokens || '');
    const hp = GV === 'flow' ? [cx, H - 12] : GV === 'timeline' ? [12, H - 10] : GV === 'iso' ? isoP(cx, cy, 16) : [cx, cy];
    if (GV === 'iso') { const h0 = isoP(cx, cy, 0); stems.push('<span class="cg-gstem" style="left:' + h0[0].toFixed(1) + 'px;top:' + (h0[1] - 16).toFixed(1) + 'px;height:16px"></span>'); }
    const hub = '<span class="cg-ghub" style="left:' + hp[0].toFixed(1) + 'px;top:' + hp[1].toFixed(1) + 'px">aide<b>' + esc(tk) + '</b></span>';
    return '<div class="cg-gal ' + esc(GV) + '">' + rings + stems.join('') + edges + dots.join('') + labels.join('') + hub + '</div>';
  }
  // the mini's detail: the compact record card for one record, over the box (the widget form shows it on a click)
  /* The hover card's contents: what this record IS, at a glance. Deliberately NOT recordCard — no buttons, no
     relations list, nothing that invites a click, because the click card is what a click is for. Colour comes
     from the family so the card and the node agree, and the text is cut short: a hover is a look, not a read. */
  function hoverCard(p) {
    const fam = p.source || 'context', sc = p.score == null ? null : +p.score;
    const txt = String((p.rec && (p.rec.text || p.rec.label)) || p.label || '').replace(/\s+/g, ' ').trim();
    return '<div class="cg-hv" style="--gc:' + esc(p.col || 'var(--cg-ac)') + '">'
      + '<div class="cg-hv-t">' + esc(String(p.label || p.id || '').slice(0, 88)) + '</div>'
      + '<div class="cg-hv-m"><i></i>' + esc(fam)
      + (sc != null && !isNaN(sc) ? '<b>' + sc.toFixed(2) + '</b>' : '')
      + (p.tok ? '<span>' + (p.tok >= 1000 ? (p.tok / 1000).toFixed(1) + 'k' : p.tok) + ' tok</span>' : '')
      + '<em>' + (p.ghost ? 'excluded' : p.lit ? 'in the prompt' : 'not sent') + '</em></div>'
      + (txt && txt !== p.label ? '<div class="cg-hv-x">' + esc(txt.slice(0, 180)) + (txt.length > 180 ? '…' : '') + '</div>' : '')
      + '</div>';
  }
  /* ONE HOVER, BOTH GRAPHS. The card belongs to the element, but the mini is static HTML the chat drops into its
     own rail — so the behaviour is exported rather than reimplemented there. Give it a root to watch and a way
     to turn an id into a record, and it does the rest: show on a node, follow the pointer, clamp inside the box,
     hide on leave and on press. The chat's mini and the element's plot then carry the SAME tooltip, which is the
     whole point of removing the browser's default one. */
  function bindHover(root, getRec, opts) {
    if (!root || root._cgHoverBound) return; root._cgHoverBound = true;
    opts = opts || {};
    const card = document.createElement('div');
    card.className = 'cg-hover'; card.hidden = true;
    if (getComputedStyle(root).position === 'static') root.style.position = 'relative';
    root.appendChild(card);
    let cur = null;
    const hide = () => { cur = null; card.hidden = true; card.innerHTML = ''; };
    const at = (id, e) => {
      const rec = getRec(id);
      if (!rec) { hide(); return; }
      if (cur !== id) { cur = id; card.innerHTML = hoverCard(rec); card.hidden = false; }
      const r = root.getBoundingClientRect(), w = card.offsetWidth || 200, h = card.offsetHeight || 60;
      let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
      if (x + w > r.width - 4) x = Math.max(4, e.clientX - r.left - w - 14);
      if (y + h > r.height - 4) y = Math.max(4, e.clientY - r.top - h - 14);
      card.style.left = x.toFixed(0) + 'px'; card.style.top = y.toFixed(0) + 'px';
    };
    // the mini's DEFAULT face is the simple galaxy, whose marks are .cg-gd — leave it out and the hover works
    // on the expanded graph and on nothing the reader usually looks at
    const SEL = opts.sel || '.cg-node[data-id],.cg-mem[data-id],.cg-gd[data-id]';
    root.addEventListener('pointerover', (e) => { const t = e.target && e.target.closest && e.target.closest(SEL); if (t) at(t.dataset.id, e); });
    root.addEventListener('pointermove', (e) => { if (cur) { const t = e.target && e.target.closest && e.target.closest(SEL); if (t) at(t.dataset.id, e); else hide(); } });
    root.addEventListener('pointerout', (e) => { const t = e.target && e.target.closest && e.target.closest(SEL);
      const to = e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest(SEL); if (t && to === t) return; hide(); });
    root.addEventListener('pointerleave', hide);
    root.addEventListener('pointerdown', hide);
    return hide;
  }
  function miniDetail(S, id, w, h) { const o = mini(Object.assign({}, S || {}, { sel: id || null, lsel: null }), w || 262, h || 196); return o.rec ? recordCard(o.rec, { compact: true }) : ''; }
  // the mini's list: the rows for the box, the search applied
  /* opts.fam narrows the rows to ONE source, so a host can open a section of its own list in place rather than
     showing a second list of everything (defect 60). The rows, their relevance bars and their include/exclude are
     the element's own either way - only which of them are shown changes. */
  function miniList(S, opts) { opts = opts || {}; const o = mini(Object.assign({}, S || {}, { q: opts.q !== undefined ? opts.q : (S && S.q) || '' }), opts.w || 262, opts.h || 196);
    const o2 = opts.fam ? Object.assign({}, o, { list: o.list.filter((r) => String(r.source || '') === String(opts.fam)) }) : o;
    if (opts.fam) o2.listTotal = o2.list.length;
    return listHtml(o2, { compact: true, limit: opts.limit || 40, closable: opts.closable }); }

  /* ── the loop lane from the loop's events (the same stream <vera-loop-graph> reads), through families.js ── */
  function loopFromEvents(evs) {
    const none = { steps: [], stepReads: [], plan: [], run: null };
    const F = root.VeraGraphFamilies; if (!F) return none;
    const d = F.toDoc('loop', { events: evs }); if (d.error) return none;
    const caps = {}, recs = {}, exec = {}; const byId = {}; d.nodes.forEach((n) => { byId[n.id] = n; });
    d.edges.forEach((e) => { if (e.label === 'CALLS') (caps[e.from] = caps[e.from] || []).push(e.to); else if (e.label === 'RECORDS') (recs[e.from] = recs[e.from] || []).push(e.to); else if (e.label === 'EXECUTED_BY') (exec[e.from] = exec[e.from] || []).push(e.to); });
    const stepNodes = d.nodes.filter((n) => n.kind === 'step'); const idx = {}; stepNodes.forEach((n, i) => { idx[n.id] = i; });
    const pruned = new Set(d.nodes.filter((n) => n.kind === 'branch' && n.status === 'pruned').map((n) => n.group || n.id.replace(/^branch:/, '')));
    const steps = stepNodes.map((n) => { const c = (caps[n.id] || []).map((id) => byId[id] && byId[id].label).filter(Boolean); const parent = (n.parents || [])[0];
      const marks = (recs[n.id] || []).map((id) => byId[id]).filter(Boolean).map((m) => ({ kind: m.kind, status: m.status, label: m.label }));
      const branch = /^sub:/.test(n.group || '') ? '' : (n.group || '');
      return { id: n.id, label: n.label, status: n.status, cap: c.join(' · '), ms: n.rec && n.rec.ms != null ? (n.rec.ms >= 1000 ? (n.rec.ms / 1000).toFixed(1) + ' s' : n.rec.ms + ' ms') : '',
        sub: /^sub:/.test(n.group || '') && parent && idx[parent] != null ? idx[parent] : -1, branch, pruned: !!(branch && pruned.has(branch)), marks,
        ranOn: (caps[n.id] || []).map((id) => byId[id] && byId[id].rec && byId[id].rec.node).filter(Boolean) }; });
    // what each step read: the wires the cap events carried, when they name context ids
    const stepReads = steps.map((s) => (caps[s.id] || []).flatMap((id) => (byId[id] && byId[id].wires) || []).filter((w) => typeof w === 'string'));
    // the run's plan (the planner's steps, in order) and which loop steps executed each
    const plan = d.nodes.filter((n) => n.kind === 'plan').map((n) => ({ id: n.id, label: n.label, status: n.status, steps: (exec[n.id] || []).map((id) => idx[id]).filter((i) => i != null), caps: (n.rec && n.rec.caps) || [] }));
    const run = d.nodes.find((n) => n.kind === 'run');
    return { steps, stepReads, plan, run: run ? { id: run.id, label: run.label, status: run.status } : null };
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
/* an edge into a record the search faded out fades with it — it must never look brighter than its dimmest end */
vera-context-graph .cg-edge.miss{opacity:.1!important}
vera-context-graph .cg-node{position:absolute;transform:translate(-50%,-50%);border-radius:50%;cursor:pointer;background:var(--nc);transition:transform .12s;display:flex;align-items:center;justify-content:center}
vera-context-graph .cg-node:hover{transform:translate(-50%,-50%) scale(1.4)}
vera-context-graph .cg-node.sq{border-radius:3px}
vera-context-graph .cg-node.dim{filter:saturate(.4)}
vera-context-graph .cg-node.ghost{background:transparent;box-shadow:inset 0 0 0 1.5px var(--nc)}
vera-context-graph .cg-node.lit{box-shadow:0 0 0 2px var(--cg-bg),0 0 0 3.5px var(--nc),0 0 14px -2px var(--nc)}
vera-context-graph .cg-node.on{box-shadow:0 0 0 3px var(--cg-bg),0 0 0 5px var(--cg-ac)}
vera-context-graph .cg-node svg{width:62%;height:62%;display:block;color:var(--cg-bg);opacity:.92;pointer-events:none;flex-shrink:0}
vera-context-graph .cg-node.ghost svg{color:var(--nc)}
vera-context-graph .cg-node span{font-family:var(--cg-mono);font-size:7px;color:var(--cg-bg);opacity:.9;pointer-events:none;max-width:90%;overflow:hidden;white-space:nowrap}
vera-context-graph .cg-mem{position:absolute;transform:translate(-50%,-50%);width:12px;height:12px;border-radius:3px;cursor:pointer;--mc:var(--cg-ac2);background:var(--mc)}
vera-context-graph .cg-mem.msg{width:16px;height:9px;border-radius:3px}vera-context-graph .cg-mem.sess{transform:translate(-50%,-50%) rotate(45deg);border-radius:1px}vera-context-graph .cg-mem.dag{border-radius:50%;border:1.5px dashed var(--mc);box-sizing:border-box}
vera-context-graph .cg-mem.ghost{background:transparent!important;box-shadow:inset 0 0 0 1.5px var(--mc);opacity:.55}
vera-context-graph .cg-lay.fam{margin-left:2px;box-shadow:inset 0 0 0 1px var(--cg-bd)}vera-context-graph .cg-lay i.p{border-radius:999px;width:13px;height:6px}vera-context-graph .cg-lay i.d{border-radius:1px;transform:rotate(45deg)}
vera-context-graph .cg-btn.on{color:var(--cg-t1);border-color:var(--cg-ac)}
vera-context-graph .cg-mem.lit,vera-context-graph .cg-mem.on{box-shadow:0 0 0 2px var(--cg-bg),0 0 0 4px var(--cg-t1)}
vera-context-graph .cg-plate{position:absolute;clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%);background:linear-gradient(180deg,color-mix(in srgb,var(--cg-ac) 9%,transparent),color-mix(in srgb,var(--cg-ac) 3%,transparent));pointer-events:none}
vera-context-graph .cg-stem{position:absolute;width:1px;background:color-mix(in srgb,var(--cg-t3) 60%,transparent);transform:translate(-50%,0);pointer-events:none}
vera-context-graph .cg-disc{position:absolute;border-radius:50%;pointer-events:none;transform-origin:50% 50%;background:conic-gradient(from var(--a0),color-mix(in srgb,var(--c) 14%,transparent) 0 var(--a1),transparent var(--a1));-webkit-mask:radial-gradient(circle,transparent var(--m0),#000 var(--m1));mask:radial-gradient(circle,transparent var(--m0),#000 var(--m1))}
vera-context-graph .cg-region{position:absolute;font-size:8.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:600;pointer-events:none}
vera-context-graph .cg-hub{position:absolute;transform:translate(-50%,-50%);width:52px;height:52px;border-radius:50%;background:var(--cg-s1);box-shadow:0 0 0 1.5px var(--cg-ac);display:flex;flex-direction:column;align-items:center;justify-content:center;pointer-events:none}
vera-context-graph .cg-hub b{font-size:11px;font-weight:600;color:var(--cg-ac)}vera-context-graph .cg-hub span{font-family:var(--cg-mono);font-size:8px;color:var(--cg-t2)}
vera-context-graph .cg-hub.hid{display:none}
vera-context-graph .cg-loop{position:absolute;transform:translate(0,-50%);height:36px;width:100px;padding:0 8px;border-radius:6px;display:flex;flex-direction:column;align-items:flex-start;justify-content:center;gap:1px;line-height:1.15;font-family:var(--cg-mono);font-size:8.5px;background:var(--cg-s2);white-space:nowrap;cursor:pointer;box-sizing:border-box}
vera-context-graph .cg-loop b{font-weight:600;font-size:9px}vera-context-graph .cg-loop .c{font-size:7.5px;color:var(--cg-t3);max-width:84px;overflow:hidden;text-overflow:ellipsis}vera-context-graph .cg-loop .t{position:absolute;right:6px;top:4px;font-size:7.5px;color:var(--cg-t3)}
vera-context-graph .cg-loop.done{color:var(--cg-ac2);box-shadow:inset 0 0 0 1px var(--cg-ac2)}vera-context-graph .cg-loop.run{color:var(--cg-ac);box-shadow:inset 0 0 0 1.5px var(--cg-ac);background:color-mix(in srgb,var(--cg-ac) 15%,transparent)}
vera-context-graph .cg-loop.pend{color:var(--cg-t3);box-shadow:inset 0 0 0 1px var(--cg-bd2)}vera-context-graph .cg-loop.fail{color:#e06c75;box-shadow:inset 0 0 0 1px #e06c75}
vera-context-graph .cg-loop.sel{box-shadow:inset 0 0 0 1.5px var(--cg-ac),0 0 0 2px var(--cg-bg),0 0 0 3.5px color-mix(in srgb,var(--cg-ac) 45%,transparent)}
vera-context-graph .cg-loop.sub{width:88px}vera-context-graph .cg-loop.pruned{opacity:.45}vera-context-graph .cg-loop.br b::before{content:'⑂ ';color:var(--cg-t3)}
vera-context-graph .cg-loop .m{position:absolute;right:5px;bottom:2px;display:flex;gap:2px}vera-context-graph .cg-loop .m i{font-style:normal;font-size:8px;line-height:1;color:var(--cg-t3)}
vera-context-graph .cg-loop .m i.ok{color:var(--cg-ac2)}vera-context-graph .cg-loop .m i.fail{color:#e06c75}vera-context-graph .cg-loop .m i.running{color:var(--cg-ac)}
vera-context-graph .cg-lstem{position:absolute;width:1px;background:var(--cg-bd2)}
vera-context-graph .cg-plan.fail{background:#e06c75}vera-context-graph .cg-planl.fail{color:#e06c75}
vera-context-graph{--cg-est:#5aa0c8}
vera-context-graph .cg-est{position:absolute;transform:translate(-50%,-50%);width:11px;height:11px;border-radius:2px;background:var(--cg-s2);box-shadow:inset 0 0 0 1.5px var(--cg-t3);cursor:pointer}
vera-context-graph .cg-est.ok{box-shadow:inset 0 0 0 1.5px var(--cg-ac2)}vera-context-graph .cg-est.warn{box-shadow:inset 0 0 0 1.5px #e0b060;background:color-mix(in srgb,#e0b060 18%,transparent)}vera-context-graph .cg-est.err{box-shadow:inset 0 0 0 1.5px #e06c75;background:color-mix(in srgb,#e06c75 22%,transparent)}
vera-context-graph .cg-est.on{box-shadow:inset 0 0 0 1.5px var(--cg-est),0 0 0 2px var(--cg-bg),0 0 0 3.5px color-mix(in srgb,var(--cg-est) 45%,transparent)}
vera-context-graph .cg-estl{position:absolute;transform:translate(-50%,0);font-family:var(--cg-mono);font-size:7px;color:var(--cg-t3);white-space:nowrap;pointer-events:none}
vera-context-graph .cg-estg{position:absolute;font-family:var(--cg-mono);font-size:7.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--cg-t3);white-space:nowrap;pointer-events:none}
vera-context-graph .cg-edge.est{opacity:.45}vera-context-graph .cg-edge.ran{opacity:.9;height:1.5px}
vera-context-graph .cg-lay i.h{border-radius:2px}
vera-context-graph .cg-edge.exec{opacity:.5}
vera-context-graph .cg-sel{font:inherit;font-size:9.5px;height:20px;max-width:190px;border:1px solid var(--cg-bd);border-radius:5px;background:var(--cg-s2);color:var(--cg-t2)}
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
vera-context-graph .cg-rec-t{font-size:10px;line-height:1.45;color:var(--cg-t2);white-space:pre-wrap;word-break:break-word;max-height:112px;overflow-y:auto;margin-top:4px;padding-top:5px;border-top:1px solid var(--cg-bd)}
vera-context-graph .cg-rec-r a.v{color:var(--cg-ac);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}vera-context-graph .cg-rec-r a.v:hover{text-decoration:underline}
vera-context-graph .cg-rec.compact{left:6px;right:6px;bottom:6px;padding:7px 8px;gap:2px;font-size:8px;max-height:70%;overflow:hidden}vera-context-graph .cg-rec.compact .cg-rec-h{font-size:9.5px}vera-context-graph .cg-rec.compact .cg-rec-r{font-size:8px}vera-context-graph .cg-rec.compact .cg-rec-r .k{width:52px}vera-context-graph .cg-rec.compact .cg-rec-t{font-size:8px;max-height:44px;margin-top:2px;padding-top:3px}vera-context-graph .cg-rec.compact .cg-rec-a button{font-size:8px;padding:2px 6px}
vera-context-graph .cg-node.miss,vera-context-graph .cg-mem.miss{opacity:.12!important;filter:saturate(.2)}
vera-context-graph .cg-body{flex:1;min-height:0;min-width:0;display:flex}vera-context-graph .cg-body .cg-plot{flex:1;min-width:0}
/* THE SOURCE METER — the section control and the legend in one strip. A band's width is its share of the window,
   so the thing you press to fold a source away is the same thing that tells you what folding it would save.
   An OFF band keeps its place and a trace of its own colour: one that vanished could never be pressed again. */
vera-context-graph .cg-meter{display:flex;align-items:stretch;gap:2px;flex:1 1 100%;min-width:0;height:20px;border-radius:5px;overflow:hidden;background:var(--cg-s3,var(--cg-s2))}
vera-context-graph .cg-mb{position:relative;flex:1 1 0;min-width:34px;display:flex;align-items:center;gap:4px;padding:0 5px;border:0;border-radius:4px;cursor:pointer;overflow:hidden;font:inherit;font-family:var(--cg-mono);font-size:8px;color:var(--cg-t1);background:color-mix(in srgb,var(--mc) 78%,transparent)}
vera-context-graph .cg-mb:hover{filter:brightness(1.18)}
vera-context-graph .cg-mb .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
vera-context-graph .cg-mb b{font-weight:400;opacity:.75;margin-left:auto}
vera-context-graph .cg-mb i{display:none}
/* focus: the band is hatched, so "some of it" reads differently from "all of it" at a glance */
vera-context-graph .cg-mb.focus{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--mc) 78%,transparent) 0 5px,color-mix(in srgb,var(--mc) 34%,transparent) 5px 10px)}
vera-context-graph .cg-mb.off{background:color-mix(in srgb,var(--mc) 16%,transparent);color:var(--cg-t3)}
vera-context-graph .cg-mb.off .nm{text-decoration:line-through}
/* the hover card: small, above the plot, and never in the pointer's way (it would flicker as the cursor entered
   its own card and left the node) */
vera-context-graph .cg-hover{position:absolute;z-index:9;pointer-events:none;max-width:260px}
vera-context-graph .cg-hover[hidden]{display:none}
vera-context-graph .cg-hv{border:1px solid var(--cg-bd);border-left:2px solid var(--gc);border-radius:7px;background:color-mix(in srgb,var(--cg-s1) 94%,transparent);backdrop-filter:blur(6px);box-shadow:0 6px 18px rgba(0,0,0,.34);padding:6px 8px}
vera-context-graph .cg-hv-t{font-size:10.5px;line-height:1.35;color:var(--cg-t1);font-weight:600}
vera-context-graph .cg-hv-m{display:flex;align-items:center;gap:5px;margin-top:3px;font-family:var(--cg-mono);font-size:8.5px;color:var(--cg-t3)}
vera-context-graph .cg-hv-m i{width:6px;height:6px;border-radius:2px;background:var(--gc);flex:none}
vera-context-graph .cg-hv-m b{font-weight:400;color:var(--cg-t2)}
vera-context-graph .cg-hv-m em{font-style:normal;margin-left:auto;color:var(--cg-t3)}
vera-context-graph .cg-hv-x{margin-top:4px;font-size:9.5px;line-height:1.45;color:var(--cg-t3);max-height:62px;overflow:hidden}
vera-context-graph .cg-list-box{flex:none;width:236px;min-height:0;display:flex;margin:6px 10px 10px 0}vera-context-graph .cg-list-box[hidden]{display:none}
vera-context-graph .cg-list{position:absolute;top:0;right:0;bottom:0;width:236px;z-index:7;display:flex;flex-direction:column;background:color-mix(in srgb,var(--cg-s1) 92%,transparent);border-left:1px solid var(--cg-bd);backdrop-filter:blur(3px)}
vera-context-graph .cg-list-box .cg-list{position:relative;inset:auto;width:100%;border:1px solid var(--cg-bd);border-radius:8px;background:var(--cg-s1);backdrop-filter:none}
vera-context-graph .cg-list-h{flex:none;display:flex;align-items:center;gap:6px;padding:7px 9px;font-family:var(--cg-mono);font-size:9px;color:var(--cg-t3);border-bottom:1px solid var(--cg-bd)}vera-context-graph .cg-list-h span:first-child{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}vera-context-graph .cg-list-h .x{cursor:pointer;color:var(--cg-t3)}vera-context-graph .cg-list-h .x:hover{color:var(--cg-t1)}
vera-context-graph .cg-list-b{flex:1;min-height:0;overflow-y:auto;padding:4px 0}
vera-context-graph .cg-row{display:grid;grid-template-columns:8px minmax(0,1fr) 44px 30px 16px 16px;grid-template-areas:"d l b t p x" ". s s s s s";align-items:center;gap:2px 6px;padding:5px 9px;cursor:pointer;font-size:10px;color:var(--cg-t1);border-left:2px solid transparent}
vera-context-graph .cg-row:hover{background:var(--cg-s2)}vera-context-graph .cg-row.on{border-left-color:var(--rc);background:var(--cg-s2)}vera-context-graph .cg-row.excl{opacity:.5}vera-context-graph .cg-row.lit b{color:var(--cg-t1)}
vera-context-graph .cg-row .dot{grid-area:d;width:8px;height:8px;border-radius:50%;background:var(--rc)}vera-context-graph .cg-row.excl .dot{background:transparent;box-shadow:inset 0 0 0 1.5px var(--rc)}
vera-context-graph .cg-row b{grid-area:l;font-weight:500;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--cg-t2)}
vera-context-graph .cg-row .bar{grid-area:b;height:4px;border-radius:2px;background:var(--cg-bd);overflow:hidden}vera-context-graph .cg-row .bar i{display:block;height:100%;background:var(--rc);border-radius:2px}
vera-context-graph .cg-row .tok{grid-area:t;font-family:var(--cg-mono);font-size:8.5px;color:var(--cg-t3);text-align:right}
vera-context-graph .cg-row .pv{grid-area:p}vera-context-graph .cg-row button{grid-area:x;font:inherit;font-size:9px;line-height:1;width:16px;height:16px;padding:0;border:none;border-radius:3px;background:transparent;color:var(--cg-t3);cursor:pointer}vera-context-graph .cg-row button:hover{background:var(--cg-bd);color:var(--cg-t1)}
vera-context-graph .cg-row small{grid-area:s;font-size:9px;line-height:1.35;color:var(--cg-t3);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* the row's detail block: the record as it actually is. The body is HELD by a max-height and scrolls, rather
   than being cut off mid-word — a record you cannot finish reading is the thing this replaces. */
vera-context-graph .cg-row-d{grid-column:1/-1;margin:3px 0 1px;display:flex;flex-direction:column;gap:3px}
vera-context-graph .cg-row-m{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-family:var(--cg-mono);font-size:8px;color:var(--cg-t3)}
vera-context-graph .cg-row-m .fam{color:var(--rc,var(--cg-t2))}
vera-context-graph .cg-row-m .in{color:var(--cg-ac)}
vera-context-graph .cg-row-m .out{color:var(--cg-t3);text-decoration:line-through}
vera-context-graph .cg-row-x{font-size:9.5px;line-height:1.5;color:var(--cg-t2);white-space:pre-wrap;word-break:break-word;max-height:132px;overflow-y:auto;padding:4px 6px;border-left:2px solid color-mix(in srgb,var(--rc,var(--cg-bd)) 45%,transparent);background:color-mix(in srgb,var(--cg-s2) 60%,transparent);border-radius:0 5px 5px 0}
vera-context-graph .cg-row-u{font-family:var(--cg-mono);font-size:8px;color:var(--cg-ac2);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
vera-context-graph .cg-row-u:hover{text-decoration:underline}
vera-context-graph .cg-list-e{padding:14px 10px;font-size:10px;color:var(--cg-t3);text-align:center}
vera-context-graph .cg-srch{font:inherit;font-size:9.5px;height:20px;width:118px;padding:0 7px;border:1px solid var(--cg-bd);border-radius:5px;background:var(--cg-s2);color:var(--cg-t1);outline:none}vera-context-graph .cg-srch:focus{border-color:var(--cg-ac)}vera-context-graph .cg-srch::placeholder{color:var(--cg-t3)}
vera-context-graph .cg-lay.et{height:18px;font-size:8.5px;padding:0 7px;text-transform:lowercase;color:var(--cg-t2)}vera-context-graph .cg-lay.et i{width:10px;height:1.5px;border-radius:0;background:currentColor}vera-context-graph .cg-lay.et:not(.on){opacity:.45;text-decoration:line-through}vera-context-graph .cg-lay.et.on{box-shadow:inset 0 0 0 1px var(--cg-bd)}
vera-context-graph .cg-layers .sep{width:1px;height:14px;background:var(--cg-bd);margin:0 4px}
vera-context-graph .cg-frames{flex-shrink:0;display:flex;align-items:center;gap:4px;padding:6px 10px 0;overflow-x:auto;font-family:var(--cg-mono);font-size:9px;color:var(--cg-t3)}vera-context-graph .cg-frames[hidden]{display:none}
vera-context-graph .cg-frame{flex:none;display:inline-flex;align-items:center;gap:5px;height:18px;padding:0 8px;border-radius:999px;border:1px solid var(--cg-bd);background:var(--cg-s2);color:var(--cg-t2);font:inherit;cursor:pointer}vera-context-graph .cg-frame b{font-weight:400;color:var(--cg-t3)}vera-context-graph .cg-frame.on{color:var(--cg-t1);border-color:var(--cg-ac);box-shadow:inset 0 0 0 1px var(--cg-ac)}vera-context-graph .cg-frame.live i{width:6px;height:6px;border-radius:50%;background:var(--cg-ac2)}
vera-context-graph .cg-rec-a{display:flex;gap:6px;margin-top:5px}
vera-context-graph .cg-rec-a button{font:inherit;font-size:10px;color:var(--cg-t2);background:var(--cg-s1);border:none;border-radius:4px;padding:4px 8px;cursor:pointer;box-shadow:0 0 0 1px var(--cg-bd)}
vera-context-graph .cg-rec-a button.pri{color:var(--cg-bg);background:var(--cg-ac);box-shadow:none}
vera-context-graph .cg-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--cg-t3);font-size:10.5px;line-height:1.5;padding:20px;pointer-events:none}
/* the families in the plot: the memory ring (dashed, on the floor in iso), its kind marks; the loop's steps as a chain, a
   standing column or a lane; the plan's diamonds in their sector; the pins the hub and the column stand on */
vera-context-graph .cg-ring.mem{box-shadow:none;border:1px dashed color-mix(in srgb,var(--cg-ac2) 60%,transparent)}
vera-context-graph .cg-ring.iso{transform:translate(-50%,-50%) var(--tf,none)}
vera-context-graph .cg-memg{position:absolute;transform:translate(-50%,-50%);font-family:var(--cg-mono);font-size:7.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--cg-ac2);white-space:nowrap;pointer-events:none;opacity:.85}
vera-context-graph .cg-mem.ghost{opacity:var(--op,.55)}
vera-context-graph .cg-step{position:absolute;transform:translate(-50%,-50%);width:14px;height:14px;border-radius:50%;background:var(--cg-s2);box-shadow:0 0 0 1.5px var(--cg-ac);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2}
vera-context-graph .cg-step svg{width:9px;height:9px;color:var(--cg-ac);display:block;pointer-events:none}
vera-context-graph .cg-step.done{box-shadow:0 0 0 1.5px var(--cg-ac2)}vera-context-graph .cg-step.done svg{color:var(--cg-ac2)}
vera-context-graph .cg-step.pend{box-shadow:0 0 0 1px var(--cg-bd2);opacity:.6}vera-context-graph .cg-step.pend svg{color:var(--cg-t3)}
vera-context-graph .cg-step.fail{box-shadow:0 0 0 1.5px #e06c75;background:color-mix(in srgb,#e06c75 25%,var(--cg-s2))}vera-context-graph .cg-step.fail svg{color:#e06c75}
vera-context-graph .cg-step.run::after{content:"";position:absolute;inset:-5px;border-radius:inherit;border:1.5px solid var(--cg-ac);opacity:.5;animation:cg-pulse 1.6s ease-out infinite}
vera-context-graph .cg-step.sel{box-shadow:0 0 0 2px var(--cg-ac),0 0 0 6px color-mix(in srgb,var(--cg-ac) 22%,transparent)}
vera-context-graph .cg-step.sub{width:11px;height:11px}vera-context-graph .cg-step.pruned{opacity:.35}
vera-context-graph .cg-stepl{position:absolute;transform:translate(-50%,0);font-family:var(--cg-mono);font-size:7.5px;color:var(--cg-t3);white-space:nowrap;pointer-events:none;max-width:84px;overflow:hidden;text-overflow:ellipsis}
@keyframes cg-pulse{0%{transform:scale(.7);opacity:.6}100%{transform:scale(1.35);opacity:0}}
vera-context-graph .cg-pnode{position:absolute;transform:translate(-50%,-50%) rotate(45deg);width:9px;height:9px;border-radius:1px;background:var(--cg-t3);z-index:2}
vera-context-graph .cg-pnode.done{background:var(--cg-ac2)}vera-context-graph .cg-pnode.run{background:var(--cg-ac);box-shadow:0 0 0 2px var(--cg-bg),0 0 0 3.5px color-mix(in srgb,var(--cg-ac) 45%,transparent)}vera-context-graph .cg-pnode.fail{background:#e06c75}
vera-context-graph .cg-pin{position:absolute;width:1px;transform:translateX(-.5px);background:linear-gradient(180deg,color-mix(in srgb,var(--c) 85%,transparent),color-mix(in srgb,var(--c) 35%,transparent));pointer-events:none;z-index:1}
vera-context-graph .cg-pin::after{content:"";position:absolute;left:-2.5px;bottom:-2.5px;width:5px;height:5px;border-radius:50%;background:var(--c);box-shadow:0 0 0 1.5px var(--cg-s1)}
vera-context-graph .cg-pin.col{width:2px;transform:translateX(-1px)}vera-context-graph .cg-pin.col::after{left:-4px;bottom:-4px;width:8px;height:8px;border-radius:2px}
vera-context-graph .cg-edge.then{height:1.5px;opacity:.5}vera-context-graph .cg-edge.then.lit{opacity:.9}
vera-context-graph .cg-edge.read{height:1.5px;opacity:.85}vera-context-graph .cg-edge.next{height:1px;opacity:.4}
vera-context-graph .cg-edge.exec.lit{opacity:.9;height:1.5px}
vera-context-graph .cg-edge.mem.spine{border-top-style:solid;opacity:.85}vera-context-graph .cg-edge.mem.hub{opacity:.35}
vera-context-graph .cg-edge.same{height:0;border-top:1px dotted var(--cg-bd2);background:none!important;opacity:.6}
vera-context-graph .cg-hubt{position:absolute;transform:translate(-50%,0);font-family:var(--cg-mono);font-size:9px;color:var(--cg-t3);white-space:nowrap;pointer-events:none;letter-spacing:.02em}
.cg-mini .cg-hubt{font-size:8.5px}
vera-context-graph .cg-edge.spoke{height:1px;opacity:.28}vera-context-graph .cg-edge.spoke.lit{height:1px;opacity:.55}
vera-context-graph .cg-list-h .all{font:inherit;font-size:8.5px;height:16px;padding:0 6px;border:1px solid var(--cg-bd);border-radius:4px;background:var(--cg-s2);color:var(--cg-t2);cursor:pointer;flex:none}vera-context-graph .cg-list-h .all:hover{color:var(--cg-t1)}
vera-context-graph .cg-rec-a a.lnk{font-size:10px;color:var(--cg-ac);text-decoration:none;padding:4px 6px;border-radius:4px;box-shadow:0 0 0 1px var(--cg-bd);background:var(--cg-s1)}vera-context-graph .cg-rec-a a.lnk:hover{text-decoration:underline}
vera-context-graph .cg-rec-a{flex-wrap:wrap}
/* the quad view: four cells inside the tracks, a hairline between them, a caption in each */
vera-context-graph .cg-div{position:absolute;background:var(--cg-bd);pointer-events:none}
vera-context-graph .cg-region.cell{font-size:8px;opacity:.8}
vera-context-graph .cg-hub.cell{width:30px;height:30px}vera-context-graph .cg-hub.cell span{display:none}vera-context-graph .cg-hub.cell b{font-size:8px}
/* the mixer: a family chip carries its level — off · focus · all — as three bars */
vera-context-graph .cg-lay .lv{display:inline-flex;gap:2px;margin-left:2px}vera-context-graph .cg-lay .lv b{width:4px;height:8px;border-radius:1px;background:var(--cg-bd2);padding:0}
vera-context-graph .cg-lay .lv b.on{background:currentColor}
vera-context-graph .cg-lay.fam:not(.on){opacity:.55}
/* the mini: the same classes in a small box — the widget form's face */
/* the design's mini galaxy (Canvas.dc.html .gal/.gd/.gring/.glb/.ghub), at the element's own tokens. The label
   is a little larger than the board's 7.5px: at this size it has to be read, not just seen (defect 60). */
.cg-gal{position:absolute;inset:0;border-radius:8px;background:radial-gradient(circle at 50% 52%,color-mix(in srgb,var(--cg-ac) 9%,transparent),transparent 60%);overflow:hidden}
.cg-gal .cg-gring{position:absolute;border-radius:50%;box-shadow:0 0 0 1px color-mix(in srgb,var(--cg-bd2) 70%,transparent);transform:translate(-50%,-50%);pointer-events:none}
.cg-gal .cg-gd{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--cg-s2);box-shadow:0 0 0 1.2px var(--c);transform:translate(-50%,-50%);cursor:pointer}
.cg-gal .cg-gd.lit{background:var(--c)}
.cg-gal .cg-gd.hollow{background:transparent;box-shadow:0 0 0 1px var(--c)}
.cg-gal .cg-gd:hover{box-shadow:0 0 0 1.2px var(--c),0 0 10px -2px var(--c)}
/* iso stands every mark above the plane on a pin, the way the exploded scene grounds its items */
.cg-gal .cg-gstem{position:absolute;width:1px;background:color-mix(in srgb,var(--cg-t2) 45%,transparent);transform:translateX(-50%);pointer-events:none}
.cg-gal .cg-gstem::after{content:'';position:absolute;left:-2px;bottom:-1px;width:5px;height:3px;border-radius:50%;background:color-mix(in srgb,var(--cg-t2) 55%,transparent)}
.cg-gal.iso .cg-gring{transform:translate(-50%,-50%) scaleY(.56)}
.cg-gal .cg-gedge{position:absolute;height:1px;background:var(--c);transform-origin:0 50%;opacity:.55;pointer-events:none}
.cg-gal .cg-gedge.faint{opacity:.18}
/* a bowed connector: the same line, curved away from the ones beside it so a fan reads as a fan (defect 60) */
.cg-gal .cg-gcur{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none}
.cg-gal .cg-gcur path{fill:none;stroke:var(--c);stroke-width:1;opacity:.55;vector-effect:non-scaling-stroke}
.cg-gal .cg-gcur.mem path{stroke-dasharray:3 2}
.cg-gal .cg-glb{position:absolute;font-family:var(--cg-mono);font-size:8.5px;white-space:nowrap;opacity:.85;pointer-events:none;transform:translate(6px,-50%)}
/* a name on the left of the plot reads outward too, so it never runs back over the middle */
.cg-gal .cg-glb.l{transform:translate(-100%,-50%) translateX(-6px)}
.cg-gal .cg-ghub{position:absolute;transform:translate(-50%,-50%);width:26px;height:26px;border-radius:50%;background:var(--cg-s2);box-shadow:0 0 0 1.2px var(--cg-ac),0 0 14px color-mix(in srgb,var(--cg-ac) 35%,transparent);display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:var(--cg-mono);font-size:7.5px;color:var(--cg-t2);line-height:1}
.cg-gal .cg-ghub b{font-size:7px;color:var(--cg-t3);font-weight:400}
.cg-mini{display:block;position:relative;overflow:hidden;--cg-bg:var(--bg0,#0e0f12);--cg-s1:var(--bg1,#15171c);--cg-s2:var(--bg2,#1b1e25);--cg-bd:var(--border,#2a2e37);--cg-bd2:color-mix(in srgb,var(--border,#2a2e37) 70%,var(--fg,#ddd));--cg-t1:var(--fg,#e6e6e6);--cg-t2:var(--dim,#aaa);--cg-t3:var(--dim2,#777);--cg-ac:var(--acc,#7c9cff);--cg-ac2:var(--acc2,#5ec9a0);--cg-est:#5aa0c8;--cg-mono:var(--mono,ui-monospace,monospace);font-size:8px;color:var(--cg-t1)}
.cg-mini .cg-in{position:absolute;inset:0}.cg-mini .cg-lanes{position:absolute;inset:0;pointer-events:none}
.cg-mini .cg-hub{width:24px;height:24px}.cg-mini .cg-hub b{font-size:9px}.cg-mini .cg-hub span{display:none}
.cg-mini .cg-loop{width:30px;height:11px;padding:0 3px;font-size:6px;border-radius:3px}.cg-mini .cg-loop b{font-size:6px}.cg-mini .cg-loop .c,.cg-mini .cg-loop .t,.cg-mini .cg-loop .m{display:none}
.cg-mini .cg-mem{width:5px;height:5px;border-radius:1px}.cg-mini .cg-mem.msg{width:7px;height:4px}.cg-mini .cg-est{width:5px;height:5px}
.cg-mini .cg-plan{width:5px;height:5px}/* the rim labels name the sources and have to be read (defect 60); the rest stay small - they only appear on the
   detailed face, which has far more on it */
.cg-mini .cg-slbl{font-size:8.5px;letter-spacing:.02em}
.cg-mini .cg-planl,.cg-mini .cg-estl,.cg-mini .cg-estg,.cg-mini .cg-region,.cg-mini .cg-memg,.cg-mini .cg-stepl{font-size:6.5px;letter-spacing:.04em}
.cg-mini .cg-step{width:7px;height:7px}.cg-mini .cg-step svg{width:4px;height:4px}.cg-mini .cg-pnode{width:5px;height:5px}
.cg-mini .cg-node svg{display:none}.cg-mini .cg-node span{display:none}.cg-mini .cg-lstem{display:none}
/* The records as a SECTION of a host's menu rather than a drawer over the plot: the same rows and the same
   --cg-* tokens (both declared on .cg-mini), flowing down the column instead of floating in a fixed box. Without
   this wrapper the rows are laid out by nothing at all and run together (Notes/42 defect 60). */
.cg-mini.cg-recs{height:auto;overflow:visible}
.cg-mini.cg-recs .cg-list{position:relative;inset:auto;width:100%;max-height:320px;border:none;border-radius:0;background:transparent;backdrop-filter:none}
.cg-mini.cg-recs .cg-list-b{max-height:288px}
.cg-mini .cg-list{width:100%;left:0;border-left:none;font-size:10px}.cg-mini .cg-list-h{padding:5px 8px;font-size:9px}.cg-mini .cg-row{padding:3px 8px;font-size:10px;grid-template-columns:6px minmax(0,1fr) 30px 22px 12px 12px;gap:1px 4px}.cg-mini .cg-list-h .all{font-size:7.5px;height:13px;padding:0 4px}.cg-mini .cg-rec-a a.lnk{font-size:8px;padding:2px 4px}.cg-mini .cg-row .dot{width:6px;height:6px}.cg-mini .cg-row .tok{font-size:7px}.cg-mini .cg-row button{width:12px;height:12px;font-size:8px}
.cg-mini .cg-rec{pointer-events:auto}
`.replace(/vera-context-graph(?=[ {.])/g, ':is(vera-context-graph,.cg-mini)');
  function ensureCss(doc) { doc = doc || document; if (doc.getElementById('vera-context-graph-css')) return; const s = doc.createElement('style'); s.id = 'vera-context-graph-css'; s.textContent = CSS; (doc.head || doc.documentElement).appendChild(s); }
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  /* ── the element ─────────────────────────────────────────────────────────────────────────────────── */
  if (typeof HTMLElement !== 'undefined' && root.customElements && !root.customElements.get('vera-context-graph')) {
    class VeraContextGraph extends HTMLElement {
      constructor() { super(); this._S = { view: 'galaxy', nodes: [], edges: [], focus: [], reads: {}, stepReads: [], loop: [], plan: [], runPlan: [], run: null, runs: [], runSel: '', dag: [], estate: { nodes: [], edges: [] }, memory: [], memEdges: [], memHide: null, memColor: null, edgeColor: null, allEdges: false, layersOff: new Set(['estate']), mix: { loop: 'focus', plan: 'focus' }, related: true, sel: null, lsel: null, pan: { x: 0, y: 0, z: 1 }, color: null, turn: '', q: '', list: false, frames: [], frame: null, edgesOff: new Set() }; this._evs = []; this._raf = 0; this._drag = null; }
      connectedCallback() {
        ensureCss(this.ownerDocument); if (this._built) { this._schedule(); return; } this._built = true;
        const a = this.getAttribute('view'); if (a) this._S.view = a;
        this.innerHTML = '<div class="cg-hd"><h2>Context graph</h2><span class="lbl" data-r="tok"></span><select class="cg-sel" data-r="runs" hidden title="The run in the lane — this session\'s, or any recorded run"></select><span class="sp"></span><span class="cg-seg" data-r="views" title="The unified graph\'s layouts, here"></span><button class="cg-btn" data-a="alledges" data-r="alledges" title="Draw every relation, not only the ones that touch the prompt">All edges</button><input class="cg-srch" data-r="q" type="search" placeholder="⌕ find a record" title="Filter the list and dim what does not match in the plot — label, text, source, type, tags"><span class="lbl" data-r="hits"></span><button class="cg-btn" data-a="list" data-r="list" title="The records as a list beside the plot">List</button><span class="lbl" data-r="zoom">100%</span><button class="cg-btn" data-a="fit" title="Back to the whole graph">Fit</button><button class="cg-btn" data-a="collapse" title="Fold the graph back into the quick menu">Collapse</button></div>'
          + '<div class="cg-key"><span><b>angle</b> = source</span><span><b>distance</b> = lower relevance</span><span><b>area</b> = tokens</span><span><b>hollow</b> = related, not injected</span></div>'
          + '<div class="cg-layers" data-r="layers"></div><div class="cg-frames" data-r="frames" hidden></div><div class="cg-body"><div class="cg-plot" data-r="plot"><div class="cg-in" data-r="in"></div><div data-r="lanes"></div><div class="cg-hover" data-r="hover" hidden></div></div><div class="cg-list-box" data-r="listbox" hidden></div></div>';
        this._r = {}; this.querySelectorAll('[data-r]').forEach((el) => { this._r[el.dataset.r] = el; });
        this._r.views.innerHTML = VIEWS.map((v) => '<button data-v="' + v[0] + '" title="' + esc(v[2]) + '">' + v[1] + '</button>').join('');
        this.addEventListener('click', (e) => this._click(e));
        this._r.q.addEventListener('input', () => { this._S.q = this._r.q.value; this._schedule(); });
        this._r.q.addEventListener('keydown', (e) => { if (e.key === 'Escape') { this._r.q.value = ''; this._S.q = ''; this._schedule(); } e.stopPropagation(); });
        this.addEventListener('change', (e) => { const s = e.target && e.target.closest && e.target.closest('select[data-r="runs"]'); if (s) this.dispatchEvent(new CustomEvent('vera:ctx:run', { detail: { session_id: s.value }, bubbles: true })); });
        const plot = this._r.plot;
        // ctrl (or Command, or a pinch) zooms the plot; a plain wheel scrolls the page past it (defect 75)
      plot.addEventListener('wheel', (e) => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); const r = plot.getBoundingClientRect(); const qx = e.clientX - (r.left + r.width / 2), qy = e.clientY - (r.top + r.height / 2); const p = this._S.pan; const nz = Math.max(0.5, Math.min(4, p.z * (e.deltaY > 0 ? 0.88 : 1.14))), k = nz / p.z; this._S.pan = { z: nz, x: qx - (qx - p.x) * k, y: qy - (qy - p.y) * k }; this._schedule(); }, { passive: false });
        /* THE HOVER CARD, through the shared binder — the same one the chat puts on its mini, so both graphs
           carry one tooltip rather than two that drift. The record card (a click, with Focus turn / Zoom to /
           Preview) is unchanged and still wins: the binder hides on pointerdown. */
        this._hoverHide = bindHover(plot, (id) => this._last && this._last.pos[id]) || (() => {});
        plot.addEventListener('pointerdown', (e) => { if (e.button || (e.target.closest && e.target.closest('.cg-rec,.cg-list,.cg-node,.cg-mem,.cg-loop,button'))) return; e.preventDefault(); this._drag = { x0: e.clientX, y0: e.clientY, px: this._S.pan.x, py: this._S.pan.y, id: e.pointerId, moved: false }; });
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
      _loopRefresh() { const L = loopFromEvents(this._evs); this._S.loop = L.steps; this._S.runPlan = L.plan; this._S.run = L.run; if (!this._S.stepReadsPinned) this._S.stepReads = L.stepReads; this._schedule(); }
      setPlan(goals) { this._S.plan = planFromGoals(goals); this._schedule(); }
      // the runs the picker offers: [{session_id, goal, status}] (the host's list); current = the one in the lane
      // the estate: the topology snapshot ({nodes:[{id,label,kind,status,detail,temp_c}], edges:[{from,to,kind}]}), through families.js
      setEstate(snapshot) { const F = root.VeraGraphFamilies; const d = F && snapshot ? F.toDoc('estate', snapshot) : null; this._S.estate = d && !d.error ? { nodes: d.nodes, edges: d.edges } : { nodes: [], edges: [] }; this._schedule(); }
      setRuns(list, o) { o = o || {}; this._S.runs = (Array.isArray(list) ? list : []).filter((r) => r && r.session_id); if (o.current != null) this._S.runSel = String(o.current); this._schedule(); }
      // the rail's Memory graph: the session's records and their relations (the same data, drawn here on the arc)
      setMemory(nodes, edges, o) { o = o || {}; const S = this._S; S.memory = Array.isArray(nodes) ? nodes : []; S.memEdges = Array.isArray(edges) ? edges : []; if (o.color) S.memColor = o.color; if (o.edgeColor) S.edgeColor = o.edgeColor; if (o.hide) S.memHide = o.hide; this._schedule(); }
      // the rail's DAG graph: the run's planned cap chain, as the loop lane while no loop is live
      setDag(nodes, edges) { const d = dagSteps(nodes, edges); this._S.dag = d.steps; this._S.dagEdges = d.edges; this._schedule(); }
      // the mixer: a family's level — off · focus · all (layersOff mirrors "off", so the host's reads of it still hold)
      mix(fam, level) { const S = this._S; if (fam && level) { S.mix[fam] = level; if (level === 'off') S.layersOff.add(fam); else S.layersOff.delete(fam); this._schedule(); } return fam ? mixOf(S, fam) : Object.assign({}, S.mix); }
      allEdges(on) { if (on != null) { this._S.allEdges = !!on; this._schedule(); } return this._S.allEdges; }
      // the turns' snapshots (the chat's CTX_FRAMES: [{id, label, ts, nodes, edges}]); active picks one to view, null = live
      setFrames(list, o) { o = o || {}; this._S.frames = (Array.isArray(list) ? list : []).filter((f) => f && f.id != null); if (o.active !== undefined) this._S.frame = o.active == null ? null : o.active; if (this._S.frame != null && !this._S.frames.some((f) => String(f.id) === String(this._S.frame))) this._S.frame = null; this._schedule(); }
      frame(id) { if (id !== undefined) { this._S.frame = id == null ? null : id; this._S.sel = null; this._schedule(); this.dispatchEvent(new CustomEvent('vera:ctx:frame', { detail: { id: this._S.frame }, bubbles: true })); } return this._S.frame; }
      search(q) { if (q !== undefined) { this._S.q = String(q || ''); if (this._r && this._r.q) this._r.q.value = this._S.q; this._schedule(); } return this._S.q; }
      list(on) { if (on !== undefined) { this._S.list = !!on; this._schedule(); } return this._S.list; }
      edgeType(name, on) { const S = this._S; if (name && on !== undefined) { if (on) S.edgesOff.delete(name); else S.edgesOff.add(name); this._schedule(); } return name ? !S.edgesOff.has(name) : [...S.edgesOff]; }
      view(name) { if (name && VIEWS.some((v) => v[0] === name)) { this._S.view = name; this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); } return this._S.view; }
      fit() { this._S.pan = { x: 0, y: 0, z: 1 }; this._schedule(); }
      select(id) { this._S.sel = id || null; this._schedule(); }
      // pan the plot so the record sits at its centre, at zoom z (at least 1.6, or the current zoom if larger)
      zoomTo(id, z) { const p = this._last && this._last.pos[id]; if (!p) return false; const plot = this._r.plot; const W = Math.max(200, plot.clientWidth || 600), H = Math.max(160, plot.clientHeight || 500); this._S.pan = panTo(p, W, H, z || Math.max(this._S.pan.z || 1, 1.6)); this._schedule(); return true; }
      state() { return this._S; }
      // screen positions of the drawn records (for the runs to the message the host draws)
      positions() { const out = []; this.querySelectorAll('.cg-node:not(.dup),.cg-mem:not(.dup)').forEach((el) => { const r = el.getBoundingClientRect(); const id = el.dataset.id; const p = this._last && this._last.pos[id]; if (!p) return; out.push({ id, x: r.left + r.width / 2, y: r.top + r.height / 2, rim: r.width / 2, col: p.col, source: p.source, lit: p.lit, ghost: p.ghost, label: p.label, by: p.by || 'u' }); }); return out; }
      // ── render ──
      _schedule() { if (this._raf || !this._built) return; this._raf = (root.requestAnimationFrame || setTimeout)(() => { this._raf = 0; this._render(); }); }
      _click(e) {
        const t = e.target; const b = t.closest && t.closest('button[data-v]'); if (b) { this.view(b.dataset.v); return; }
        const a = t.closest && t.closest('[data-a]'); if (a) { const S = this._S; const k = a.dataset.a;
          if (k === 'fit') this.fit(); else if (k === 'collapse') { this.dispatchEvent(new CustomEvent('vera:ctx:collapse', { bubbles: true })); } else if (k === 'alledges') { S.allEdges = !S.allEdges; this._schedule(); this.dispatchEvent(new CustomEvent('vera:ctx:alledges', { detail: { on: S.allEdges }, bubbles: true })); }           // a SOURCE cycles the same three levels a family does now: off · focus · all. It used to flip on/off,
          // so the only way to quieten a noisy source was to lose it. mix() is the one path for both.
          else if (k === 'layer') { const s = a.dataset.s; const cur = mixOf(S, s);
            this.mix(s, cur === 'off' ? 'focus' : cur === 'focus' ? 'all' : 'off'); }
          else if (k === 'related') { S.related = !S.related; this._schedule(); } else if (k === 'close') { S.sel = null; this._schedule(); }
          else if (k === 'zoom') { this.zoomTo(a.dataset.id); } else if (k === 'preview') { this.dispatchEvent(new CustomEvent('vera:ctx:preview', { detail: { id: a.dataset.id, url: a.dataset.url }, bubbles: true })); }
          else if (k === 'incl-all' || k === 'excl-all') { this.dispatchEvent(new CustomEvent('vera:ctx:toggle-all', { detail: { included: k === 'incl-all' }, bubbles: true })); }
          else if (k === 'list') { this.list(!S.list); } else if (k === 'etype') { this.edgeType(a.dataset.t, !!a.dataset.off); } else if (k === 'frame') { this.frame(a.dataset.id === '' ? null : a.dataset.id); }
          else if (k === 'toggle') { this.dispatchEvent(new CustomEvent('vera:ctx:toggle', { detail: { id: a.dataset.id }, bubbles: true })); }
          else if (k === 'open') { const r = this._last && this._last.rec; this.dispatchEvent(new CustomEvent('vera:ctx:pick', { detail: { id: a.dataset.id, open: true, family: r && r.id === a.dataset.id ? r.family : 'context', rec: r && r.id === a.dataset.id ? r.rec : null }, bubbles: true })); }
          else if (k === 'turn') { this.dispatchEvent(new CustomEvent('vera:ctx:focus-turn', { detail: { mid: a.dataset.mid }, bubbles: true })); }
          return; }
        const row = t.closest && t.closest('.cg-row'); if (row) { const S = this._S; S.sel = S.sel === row.dataset.id ? null : row.dataset.id; S.lsel = null; if (S.sel) this.zoomTo(S.sel); else this._schedule(); this.dispatchEvent(new CustomEvent('vera:ctx:pick', { detail: { id: row.dataset.id } , bubbles: true })); return; }
        const n = t.closest && t.closest('.cg-node,.cg-mem,.cg-est'); if (n) { const S = this._S; S.sel = S.sel === n.dataset.id ? null : n.dataset.id; S.lsel = null; this._schedule(); this.dispatchEvent(new CustomEvent('vera:ctx:pick', { detail: { id: n.dataset.id }, bubbles: true })); return; }
        const l = t.closest && t.closest('.cg-loop,.cg-step'); if (l) { const i = +l.dataset.i; const S = this._S; S.lsel = S.lsel === i ? null : i; S.sel = null; this._schedule(); }
      }
      _render() {
        const S = this._S, plot = this._r.plot; const W = plot.clientWidth || 600, H = plot.clientHeight || 500;
        // the iso view's projection through the shared ISO library when it is on the page (the same maths otherwise);
        // compute() picks the tilt from the column and asks for the projection at it
        const isoMake = root.VeraISO && typeof root.VeraISO.proj === 'function' ? (tilt, k) => root.VeraISO.proj(tilt, 45, k, true) : null;
        const o = compute(Object.assign({}, S, { isoMake }), W, H); this._last = o;
        const p = S.pan; this._r.in.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px) scale(' + p.z + ')';
        this._r.zoom.textContent = Math.round(p.z * 100) + '%';
        this._r.tok.textContent = (o.tokens ? o.tokens.toLocaleString() + ' tokens in the prompt' : '') + (o.turn ? (o.tokens ? ' · ' : '') + o.turn : '');
        this._r.views.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === o.view));
        if (this._r.alledges) this._r.alledges.classList.toggle('on', !!S.allEdges);
        if (this._r.list) this._r.list.classList.toggle('on', !!S.list);
        if (this._r.hits) this._r.hits.textContent = o.q ? o.hits + ' hit' + (o.hits === 1 ? '' : 's') : '';
        // the frames scrubber: the turns' snapshots, the live set first
        if (this._r.frames) { const fr = o.frames || []; this._r.frames.hidden = !fr.length;
          if (fr.length) this._r.frames.innerHTML = '<span>frames</span><button class="cg-frame live ' + (o.frame ? '' : 'on') + '" data-a="frame" data-id="" title="The live records"><i></i>live</button>' + fr.slice(-12).map((f) => '<button class="cg-frame ' + (f.on ? 'on' : '') + '" data-a="frame" data-id="' + esc(f.id) + '" title="' + esc(f.label + (f.ts ? ' · ' + f.ts : '') + ' · ' + f.n + ' records · click to view this turn\'s context') + '">' + esc(f.label) + '<b>' + f.n + '</b></button>').join(''); }
        if (this._r.runs) { const runs = S.runs || []; const cur = S.runSel || ''; const others = runs.filter((r) => r.session_id !== cur);
          const sig = cur + '|' + (S.run ? S.run.label + ':' + S.run.status : '') + '|' + others.map((r) => r.session_id + ':' + r.status).join(',');
          if (sig !== this._runsSig) { this._runsSig = sig; const glyph = (st) => st === 'running' ? '● ' : /error|fail|interrupted/.test(st || '') ? '✕ ' : '✓ ';
            this._r.runs.innerHTML = '<option value="' + esc(cur) + '">' + (S.run ? esc(glyph(S.run.status === 'ok' ? 'done' : S.run.status === 'fail' ? 'error' : 'running') + String(S.run.label || 'this run').slice(0, 38)) : 'this session · no run') + '</option>'
              + others.map((r) => '<option value="' + esc(r.session_id) + '">' + esc(glyph(r.status) + String(r.goal || r.session_id).slice(0, 38)) + '</option>').join('');
            this._r.runs.hidden = !(runs.length || S.run); } }
        // the mixer: a family chip cycles off → focus → all; the three bars say where it stands
        const LV = { off: 0, focus: 1, all: 2 };
        const famChip = (f) => '<button class="cg-lay fam ' + (f.on ? 'on' : '') + '" data-a="layer" data-s="' + esc(f.fam) + '" data-level="' + esc(f.level) + '" style="color:' + esc(f.col) + '" title="' + esc(f.name) + ' · ' + f.n + ' · the ' + esc(f.name) + ' graph as a family of this one — ' + (f.level === 'off' ? 'off' : f.level === 'focus' ? 'in focus: what touches the prompt' : 'everything') + ' · click to cycle off · focus · all"><i class="' + (f.name === 'plan' ? 'd' : f.name === 'estate' ? 'h' : f.name === 'memory' ? '' : 'p') + '" style="background:' + esc(f.col) + '"></i>' + esc(f.name) + '<b>' + f.n + '</b><span class="lv"><b class="' + (LV[f.level] >= 1 ? 'on' : '') + '"></b><b class="' + (LV[f.level] >= 2 ? 'on' : '') + '"></b></span></button>';
        /* THE METER IS THE SECTION CONTROL, AND THE LEGEND. The sources were a row of chips carrying a colour,
           a name and a count — three facts the meter can carry while ALSO showing what the plot cannot: how much
           of the window each source is actually spending. A band's width is its token share; pressing it cycles
           off · focus · all, the same three levels the family chips have always had.
           An off band keeps its place and a trace of its own colour: a band that vanished could never be pressed
           again, and its source would be unreachable. min-width is the one concession to geometry — a source
           holding 1% of the window is a 2px target. */
        const meterSrcs = o.srcs.concat(o.offSrcs);
        const meterTot = meterSrcs.reduce((a, s) => a + (s.tok || 0), 0) || 1;
        const LVN = { off: 'off', focus: 'in focus: only what went into the prompt', all: 'everything' };
        this._r.layers.innerHTML = (meterSrcs.length
          ? '<span class="cg-meter" role="group" aria-label="context sources">'
            + meterSrcs.map((s) => '<button class="cg-mb ' + esc(s.level) + '" data-a="layer" data-s="' + esc(s.name)
              + '" data-level="' + esc(s.level) + '" style="--mc:' + esc(s.col) + ';flex-grow:' + Math.max(1, Math.round((s.tok || 0) / meterTot * 1000))
              + '" title="' + esc(s.name + ' · ' + s.n + ' record' + (s.n === 1 ? '' : 's') + ' · ' + (s.tok || 0) + ' tokens · '
              + Math.round((s.tok || 0) / meterTot * 100) + '% of the window · ' + (LVN[s.level] || 'everything')
              + ' · press to cycle off · focus · all') + '"><i></i><span class="nm">' + esc(s.name) + '</span><b>' + s.n + '</b></button>').join('')
            + '</span>'
          : '')
          + (o.families || []).map(famChip).join('')
          /* THE RELATIONS GET A METER TOO, coded to the edges they draw. They were a row of chips carrying a
             name and a count in a uniform grey, which told you a type existed but not which lines on the plot
             were that type. Each band is now the type's own colour and is sized by how many relations it holds,
             so the legend and the drawing agree at a glance. A folded type keeps its place, faintly, exactly as
             a folded source does on the meter above. */
          + ((o.edgeTypes || []).length ? (function () {
              const ets = o.edgeTypes.slice(0, 10), tot = ets.reduce((a, t) => a + t.n, 0) || 1;
              return '<span class="cg-meter et" role="group" aria-label="relation types">'
                + ets.map((t) => '<button class="cg-mb' + (t.on ? '' : ' off') + '" data-a="etype" data-t="' + esc(t.name) + '"'
                  + (t.on ? '' : ' data-off="1"') + ' style="--mc:' + esc(t.col) + ';flex-grow:' + Math.max(1, Math.round(t.n / tot * 1000))
                  + '" title="' + esc(t.name.replace(/_/g, ' ').toLowerCase()) + ' · ' + t.n + ' relation' + (t.n === 1 ? '' : 's')
                  + ' · ' + Math.round(t.n / tot * 100) + '% of them · press to ' + (t.on ? 'fold this type away' : 'draw it again')
                  + '"><i></i><span class="nm">' + esc(t.name.replace(/_/g, ' ').toLowerCase()) + '</span><b>' + t.n + '</b></button>').join('')
                + '</span>'; })() : '')
          + (o.ghosts ? '<span style="flex:1"></span><button class="cg-lay ' + (S.related ? 'on' : '') + '" data-a="related" style="color:var(--cg-ac2)" title="Records related to this question that were not injected"><i class="s"></i>related<b>+' + o.ghosts + '</b></button>' : '');
        this._r.in.innerHTML = drawPlot(o);
        // the lanes stay put while the plot pans and zooms
        if (this._r.listbox) { this._r.listbox.hidden = !S.list; this._r.listbox.innerHTML = S.list ? listHtml(o) : ''; }
        let l = drawLanes(o);
        if (!o.cnodes.length && !o.memNodes.length) l += '<div class="cg-empty">' + (S.nodes.length ? 'Every layer is folded away — turn one back on above.' : 'The records the aide assembles for a turn appear here — send a message with context injection on.') + '</div>';
        this._r.lanes.innerHTML = l;
        this.dispatchEvent(new CustomEvent('vera:ctx:rendered', { detail: { view: o.view, tokens: o.tokens, lit: o.lit, nodes: o.cnodes.length + o.memNodes.length, mix: o.mix }, bubbles: true }));
      }
    }
    root.customElements.define('vera-context-graph', VeraContextGraph);
  }
  const api = { compute, mini, miniHtml, miniDetail, miniList, stateFrom, drawPlot, drawMiniGalaxy, drawLanes, recordCard, bindHover, hoverCard, listHtml, panTo, textOf, edgeTypeOf, loopFromEvents, planFromGoals, dagSteps, mixOf, VIEWS, FAMS, frameReads, ensureCss, kindOf, ICON, version: 6 };
  root.VeraContextGraph = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
