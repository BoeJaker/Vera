// The context graph's FAMILIES and TRACKS (the GraphViews board on the chat's column; Notes/42 defects 29 · 33 · 35):
// the memory ring grouped by kind, the loop and the plan as layers INSIDE the plot beside their tracks around the
// edges, the mixer per family (off · focus · all), the iso plate sized from the column, the QUAD view (one dataset
// drawn four ways inside the tracks), the record panel for a loop step, the mini layout for the widget form.
//   node tests/test_context_graph_families.cjs
const path = require('node:path');
const F = require(path.join(__dirname, '..', 'vera', 'graph', 'families.js'));
global.VeraGraphFamilies = F;
const G = require(path.join(__dirname, '..', 'vera', 'chat', 'context_graph_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const nodes = [
  { id: 'v1', label: 'fabric_capabilities.py', source: 'vector', score: 0.94, text: 'x'.repeat(800), included: true },
  { id: 'v2', label: 'boot log', source: 'vector', score: 0.7, text: 'x'.repeat(200), included: true },
  { id: 'g1', label: 'commit 312caef', source: 'graph', score: 0.9, type: 'dataset', included: true },
  { id: 'c1', label: 'fabric.digest', source: 'cap', score: 0.6, included: true },
  { id: 'w1', label: 'issue #41', source: 'web', score: 0.5, included: false },
  { id: 'm1', label: 'recall 1', source: 'memory', score: 0.8, included: true },
];
const edges = [{ from: 'v1', to: 'g1', label: 'CITES' }];
const sess = [{ id: 'ss1', record_type: 'session', summary: 'session', created_at: '2026-09-11T09:59:00Z', importance: 0.9 },
  { id: 'sm1', record_type: 'message', text: 'why?', created_at: '2026-09-11T10:00:00Z', source_type: 'human', importance: 0.7 }, { id: 'sm2', record_type: 'message', text: 'because', created_at: '2026-09-11T10:00:30Z', source_type: 'ai', importance: 0.6 },
  { id: 'sd1', record_type: 'dag_step', capability: 'obs.health', created_at: '2026-09-11T10:01:00Z', importance: 0.4 }, { id: 'sf1', record_type: 'fact', text: 'a fact', created_at: '2026-09-11T10:02:00Z', importance: 0.3 }, { id: 'se1', record_type: 'entity', text: 'ct126', created_at: '2026-09-11T10:03:00Z', importance: 0.5 }];
const sessEdges = [{ from_id: 'ss1', to_id: 'sm1', relation: 'SESSION_CONTENT' }, { from_id: 'sm1', to_id: 'sm2', relation: 'FOLLOWED_BY' }, { from_id: 'sm2', to_id: 'sd1', relation: 'FOLLOWED_BY' }, { from_id: 'sd1', to_id: 'sf1', relation: 'DERIVED_FROM' }, { from_id: 'sf1', to_id: 'v1', relation: 'DERIVED_FROM' }];
const L = G.loopFromEvents([{ type: 'agent_loop_v7.triage_start', goal: 'fix boot' }, { type: 'agent_loop_v7.plan', steps: [{ id: 1, title: 'recon' }, { id: 2, title: 'author' }, { id: 3, title: 'test' }] },
  { type: 'agent_loop_v7.step_start', step_id: 1, title: 'recon' }, { type: 'cap.ok', step_id: 1, tool: 'obs.provenance', wires: ['v1', 'g1'], node: 'ct126' }, { type: 'agent_loop_v7.step_done', step_id: 1, ms: 1200 },
  { type: 'agent_loop_v7.step_start', step_id: 2, title: 'author' }, { type: 'cap', step_id: 2, tool: 'code.author', wires: ['c1'] }]);
const base = { view: 'galaxy', nodes, edges, focus: ['v1', 'g1', 'm1', 'c1'], reads: { m4: ['v1', 'g1', 'm1', 'c1'] }, layersOff: new Set(), related: true, pan: { x: 0, y: 0, z: 1 },
  memory: sess, memEdges: sessEdges, memHide: new Set(['SESSION_CONTENT']), memColor: (ty) => ty === 'message' ? '#5a9e8f' : ty === 'session' ? '#fb923c' : ty === 'dag' ? '#a78bfa' : '#8a7e70',
  loop: L.steps, runPlan: L.plan, run: L.run, stepReads: L.stepReads, plan: [] };
const W = 660, H = 640;
// ── the memory ring, grouped by kind ──
const g = G.compute(base, W, H);
const cx = g.hub.x, cy = g.hub.y; const dist = (p) => Math.hypot(p.x - cx, p.y - cy);
t('the memory ring: a dashed ring of its own, the injected recall on it, the session outside it', g.rings.some((r) => r.cls === 'mem') && Math.abs(dist(g.pos.m1) - +g.rings.find((r) => r.cls === 'mem').d / 2) < 1 && dist(g.pos.sm1) > dist(g.pos.m1));
const angle = (id) => { const a = Math.atan2(g.pos[id].y - cy, g.pos[id].x - cx) * 180 / Math.PI; return a > 90 ? a - 360 : a; };   // the arm runs -195° → -15°, clockwise from the lower left
t('the session records lie along the arm grouped by kind — session, then the messages in time order, then the dag step, the fact, the entity', angle('ss1') < angle('sm1') && angle('sm1') < angle('sm2') && angle('sm2') < angle('sd1') && angle('sd1') < angle('sf1') && angle('sf1') < angle('se1'), [ 'ss1', 'sm1', 'sm2', 'sd1', 'sf1', 'se1' ].map((id) => id + ':' + angle(id).toFixed(0)).join(' '));
t('a kind mark per group, with its count', g.memLabels.map((l) => l.t).join(',') === 'session · 1,message · 2,dag · 1,fact · 1,entity · 1', JSON.stringify(g.memLabels.map((l) => l.t)));
t('the FOLLOWS spine draws solid, a derived relation dashed, the hub edge folded (the rail\'s own filter)', g.cedges.some((e) => /followed by/.test(e.title) && /\bspine\b/.test(e.cls)) && g.cedges.some((e) => /derived from/.test(e.title) && !/spine/.test(e.cls)) && !g.cedges.some((e) => /session content/.test(e.title)));
t('a session record\'s importance is its weight on the ring (opacity in by relevance)', +g.memNodes.find((n) => n.id === 'ss1').op > +g.memNodes.find((n) => n.id === 'sf1').op);
// ── the loop as a layer of the plot AND the track down the left ──
t('the loop: the track (pills down the left) and the layer (steps in the plot\'s loop sector) both drawn', g.loopNodes.length === 2 && g.stepNodes.length === 2 && g.lanes.l === 118 && g.stepNodes.every((n) => +n.x > 118));
const sa = (n) => Math.atan2(+n.y - cy, +n.x - cx) * 180 / Math.PI;
t('the loop sector is the right and bottom (40°…160°); the context keeps the left and top; the memory arm lies outside the context', g.stepNodes.every((n) => sa(n) > 40 && sa(n) < 160) && g.cnodes.every((n) => { const a = sa(n); return a < -10 || a > 160; }) && g.sectorLabels.some((s) => s.name === 'loop'));
t('THEN chains the steps; the step in the plot is wired to what it read; the pill and the step are one node (a hairline between them)', g.cedges.some((e) => e.cls.indexOf('then') === 0 && e.title === 'step 1 → step 2') && g.cedges.some((e) => /^read/.test(e.cls) && e.title === 'step 2 read fabric.digest') && g.sedges.filter((e) => e.cls === 'same').length === 2);
t('the plan: the row along the top AND its sector in the plot (diamonds, NEXT along the arc, EXECUTED_BY into the loop\'s steps)', g.planNodes.length === 3 && g.planPlot.length === 3 && g.cedges.some((e) => e.cls === 'next' && e.title === 'plan step 1 → 2') && g.cedges.some((e) => /^exec/.test(e.cls) && e.title === 'plan step 2 ran as loop step 2') && g.sectorLabels.some((s) => s.name === 'plan'));
// ── the mixer: off · focus · all per family ──
const gF = G.compute(Object.assign({}, base, { mix: { loop: 'focus', plan: 'focus', memory: 'focus' } }), W, H);
t('loop in focus: the lane keeps every step, the plot keeps the running step, its neighbours and the steps that read the prompt', gF.loopNodes.length === 2 && gF.stepNodes.length === 2 && gF.mix.loop === 'focus' && gF.families.find((f) => f.name === 'loop').level === 'focus');
const L3 = G.loopFromEvents([{ type: 'start', run_id: 'r' }, { type: 'agent_loop_v7.step_start', step_id: 1, title: 'a' }, { type: 'agent_loop_v7.step_done', step_id: 1 }, { type: 'agent_loop_v7.step_start', step_id: 2, title: 'b' }, { type: 'agent_loop_v7.step_done', step_id: 2 }, { type: 'agent_loop_v7.step_start', step_id: 3, title: 'c' }, { type: 'agent_loop_v7.step_done', step_id: 3 }, { type: 'agent_loop_v7.step_start', step_id: 4, title: 'd' }, { type: 'agent_loop_v7.step_done', step_id: 4 }, { type: 'agent_loop_v7.step_start', step_id: 5, title: 'e' }]);
const gF2 = G.compute(Object.assign({}, base, { loop: L3.steps, runPlan: [], stepReads: [], mix: { loop: 'focus' } }), W, H);
t('… five steps, none reading the prompt: the plot shows the running one and its neighbour, the lane all five', gF2.loopNodes.length === 5 && gF2.stepNodes.map((n) => n.i).join(',') === '3,4');
const gF3 = G.compute(Object.assign({}, base, { loop: L3.steps, runPlan: [], stepReads: [], mix: { loop: 'focus' }, lsel: 1 }), W, H);
t('… a step picked in the lane brings it and its neighbours into the plot', gF3.stepNodes.map((n) => n.i).join(',') === '0,1,2' && /\bsel\b/.test(gF3.stepNodes[1].cls));
t('memory in focus: only the session records that touch the prompt stay on the arm (the fact derived into v1)', gF.memNodes.map((n) => n.id).sort().join(',') === 'm1,sf1' && gF.families.find((f) => f.name === 'memory').level === 'focus');
const gO = G.compute(Object.assign({}, base, { mix: { loop: 'off', memory: 'off', plan: 'off' } }), W, H);
t('off folds the family away — no track, no layer, no chip glow; the context takes the whole circle again', gO.loopNodes.length === 0 && gO.stepNodes.length === 0 && gO.planNodes.length === 0 && gO.planPlot.length === 0 && gO.memNodes.length === 1 && gO.lanes.l === 0 && gO.lanes.t === 0 && gO.families.every((f) => !f.on) && gO.spokes.length === 4);
t('mixOf: an explicit level wins, layersOff reads as off, else all', G.mixOf({ mix: { loop: 'focus' }, layersOff: new Set(['loop', 'estate']) }, 'loop') === 'focus' && G.mixOf({ layersOff: new Set(['estate']) }, 'estate') === 'off' && G.mixOf({}, 'memory') === 'all');
// ── the iso view fills its column (defect 29) ──
const tall = G.compute(Object.assign({}, base, { view: 'iso', mix: { loop: 'off', plan: 'off' } }), 640, 700);
const wide = G.compute(Object.assign({}, base, { view: 'iso', mix: { loop: 'off', plan: 'off' } }), 900, 300);
t('a tall column steepens the tilt (up to 58°) and the plate fills the width', tall.iso.tilt > 50 && tall.iso.tilt < 59 && +tall.plate.w > 640 * 0.85 && +tall.plate.x < 0, JSON.stringify([tall.iso, tall.plate]));
const spanY = (o) => { const ys = o.cnodes.map((n) => +n.y).concat(o.memNodes.map((n) => +n.y)); return Math.max.apply(null, ys) - Math.min.apply(null, ys); };
t('… the galaxy uses the column\'s height: the nodes span more than half of it (the old tilt spanned a quarter)', spanY(tall) > 700 * 0.5, spanY(tall));
t('a wide, low plot keeps the 30° tilt and is bound by its height', Math.abs(wide.iso.tilt - 30) < 1e-6 && spanY(wide) < 300);
t('the sector discs follow the tilt; the memory ring lies on the floor with the same squash; the hub stands on a pin', tall.discs.every((d) => d.tf === 'scale(' + (tall.iso.kx * Math.SQRT2).toFixed(3) + ',' + (tall.iso.ky * Math.SQRT2).toFixed(3) + ')') && tall.rings.some((r) => /mem/.test(r.cls) && r.tf === tall.discs[0].tf) && tall.pins.some((p) => p.cls === 'hub' && +p.h > 0));
const isoL = G.compute(Object.assign({}, base, { view: 'iso', mix: { loop: 'all', plan: 'all' } }), 640, 700);
t('iso: the loop stands as a column of steps on a pin at its sector; the plan on the floor; discs for both', isoL.pins.some((p) => p.cls === 'col') && isoL.stepNodes.length === 2 && +isoL.stepNodes[1].y < +isoL.stepNodes[0].y && isoL.planPlot.length === 3 && isoL.discs.some((d) => d.name === 'loop') && isoL.discs.some((d) => d.name === 'plan'));
// ── flow and time carry the loop as a lane / a row ──
const fl = G.compute(Object.assign({}, base, { view: 'flow', mix: { loop: 'all' } }), W, H), tm = G.compute(Object.assign({}, base, { view: 'time', mix: { loop: 'all' } }), W, H);
t('flow: the loop is a lane across the foot, left to right, above the memory strip', fl.stepNodes.length === 2 && +fl.stepNodes[0].x < +fl.stepNodes[1].x && fl.stepNodes.every((n) => +n.y < Math.min.apply(null, fl.memNodes.map((m) => +m.y))) && fl.sectorLabels.some((s) => s.name === 'loop' && s.lane));
t('time: the loop is a row of its own below the sources', tm.stepNodes.length === 2 && tm.stepNodes.every((n) => +n.y > Math.max.apply(null, tm.cnodes.map((c) => +c.y))) && tm.sectorLabels.filter((s) => s.lane).map((s) => s.name).join(',') === 'vector,graph,web,cap,loop');
// ── the QUAD view (defect 35): the four at once inside the tracks ──
const q = G.compute(Object.assign({}, base, { view: 'quad', mix: { loop: 'all', plan: 'all' }, allEdges: true }), 900, 800);
t('quad: four cells, captioned, two hairlines; the tracks around them stay (the lane, the row)', q.regions.filter((r) => r.cell).map((r) => r.t).join(',') === 'galaxy,iso,flow,time' && q.dividers.length === 2 && q.loopNodes.length === 2 && q.planNodes.length === 3 && q.lanes.l === 118 && q.lanes.t === 52);
const cell = (n) => (+n.x < 118 + (900 - 118) / 2 ? 'l' : 'r') + (+n.y < 52 + (800 - 52) / 2 ? 't' : 'b');
t('… every record is drawn four times, once per cell; the copies are marked so the runs to the message use the first', q.cnodes.filter((n) => n.id === 'v1').length === 4 && new Set(q.cnodes.filter((n) => n.id === 'v1').map(cell)).size === 4 && q.cnodes.filter((n) => n.id === 'v1' && /\bdup\b/.test(n.cls)).length === 3 && !/\bdup\b/.test(q.cnodes.find((n) => n.id === 'v1').cls));
t('… the plot`s positions are the galaxy cell`s; the iso cell has its plate, discs and pins; a hub per galaxy/iso cell', cell(q.pos.v1) === 'lt' && q.plate && +q.plate.x >= 118 + (900 - 118) / 2 - 200 && q.discs.length > 0 && q.pins.length >= 2 && q.hubs.length === 2 && q.hub.hid);
t('… the lane`s reads reach the galaxy cell; the record panel and the mixer work in it', q.sedges.some((e) => /^step 2 read/.test(e.title)) && G.compute(Object.assign({}, base, { view: 'quad', sel: 'v1' }), 900, 800).rec.name === 'fabric_capabilities.py' && G.compute(Object.assign({}, base, { view: 'quad', mix: { loop: 'off' } }), 900, 800).loopNodes.length === 0);
// ── the record panel for a loop step ──
const gS = G.compute(Object.assign({}, base, { lsel: 0 }), W, H);
t('a loop step picked opens its record: status, capability, took, what it read, its plan step, where it ran', gS.rec && gS.rec.family === 'loop' && gS.rec.name === '1 · recon' && gS.rec.rows.map((r) => r.k + '=' + r.v).join(',') === 'status=ok,capability=obs.provenance,took=1.2 s,read=2 records,plan step=1 · recon,ran on=ct126' && gS.rec.rels[0] === 'read fabric_capabilities.py', JSON.stringify(gS.rec && gS.rec.rows));
// ── the DAG's own edges (setDag(nodes, edges)) ──
const D = G.dagSteps([{ id: 0, cap: 'obs.health', out: 'h', status: 'done' }, { id: 1, cap: '[parallel 2]', out: '', status: 'running' }, { id: 2, cap: 'evolve.assess', out: '', status: 'pending' }], [{ from: 0, to: 1 }, { from: 1, to: 2 }, { from: 0, to: 2 }]);
t('dagSteps: the rail\'s planned chain through families.js — status normalised, the parallel node marked, the edges kept', D.steps.length === 3 && D.steps[0].status === 'ok' && D.steps[1].status === 'running' && D.steps[1].parallel && D.steps[0].cap === 'h' && D.edges.length === 3 && D.edges[2].from === 'dag:0' && D.edges[2].to === 'dag:2');
const gDag = G.compute(Object.assign({}, base, { loop: [], runPlan: [], stepReads: [], dag: D.steps, dagEdges: D.edges, mix: { loop: 'all' } }), W, H);
t('the DAG takes the lane and the loop sector, its own skip edge drawn in the plot', gDag.loopNodes.length === 3 && gDag.stepNodes.length === 3 && gDag.cedges.some((e) => e.cls === 'then' && e.title === 'obs.health → evolve.assess') && gDag.sectorLabels.some((s) => s.name === 'dag') && gDag.regions.some((r) => /^dag · 1 of 3/.test(r.t)));
// ── the estate in focus ──
const SNAP = F.toDoc('estate', { nodes: [{ id: 'hub', label: 'Vera', kind: 'hub' }, { id: 'cat:nodes', label: 'Nodes', kind: 'category' }, { id: 'node:ct126', label: 'ct126', kind: 'node', status: 'ok' }, { id: 'node:ct121', label: 'ct121', kind: 'node', status: 'warn' }], edges: [{ from: 'hub', to: 'cat:nodes' }, { from: 'cat:nodes', to: 'node:ct126' }, { from: 'cat:nodes', to: 'node:ct121' }] });
const gE = G.compute(Object.assign({}, base, { estate: { nodes: SNAP.nodes, edges: SNAP.edges }, mix: { estate: 'focus' } }), W, H);
t('estate in focus: only the nodes a step ran on stay on the strip', gE.estNodes.length === 1 && gE.estNodes[0].id === 'estate:node:ct126' && gE.sedges.some((e) => e.cls === 'ran') && gE.regions.some((r) => r.t === 'estate · ran on'));
// ── the mini: the same layout in a small box, the tracks scaled to it ──
const m = G.mini(base, 262, 196);
t('mini: the same families — the records, the memory ring, the loop lane and layer, the plan row — inside 262 × 196', m.cnodes.length === 5 && m.memNodes.length === 7 && m.loopNodes.length === 2 && m.stepNodes.length === 2 && m.planNodes.length === 3 && m.lanes.l === Math.round(118 * 0.3) && m.lanes.t === Math.round(52 * 0.3)
  && m.cnodes.concat(m.memNodes).every((n) => +n.x >= 0 && +n.x <= 262 && +n.y >= 0 && +n.y <= 196), JSON.stringify(m.lanes));
t('mini: node sizes scale with the box', +m.cnodes.find((n) => n.id === 'v1').d < +g.cnodes.find((n) => n.id === 'v1').d);
t('stateFrom: a widget record\'s data becomes a state (rels → edges, included → focus)', (() => { const s = G.stateFrom({ nodes: [{ id: 'a', included: true }, { id: 'b', included: false }], rels: [{ from: 'a', to: 'b', kind: 'cites' }] }); return s.edges[0].label === 'cites' && s.focus.join(',') === 'a' && s.view === 'galaxy'; })());
t('miniHtml draws the layout with the element\'s classes', /class="cg-mini"/.test(G.miniHtml(base, 262, 196)) && /cg-node/.test(G.miniHtml(base, 262, 196)) && /cg-loop/.test(G.miniHtml(base, 262, 196)) && /cg-mem/.test(G.miniHtml(base, 262, 196)));
// ── families.js: the memory family's structure ──
t('families.js names the memory kinds, groups a record by kind, classes an edge', F.MEM_KINDS.indexOf('message') >= 0 && F.memGroup('dag_step') === 'dag' && F.memGroup('weird') === 'memory' && F.memEdgeClass('FOLLOWED_BY') === 'spine' && F.memEdgeClass('CAUSES') === 'structural' && F.memEdgeClass('SESSION_CONTENT') === 'hub' && F.memEdgeClass('SIMILAR') === 'inferred');
const md = F.toDoc('memory', { nodes: [{ id: 'a', record_type: 'message' }, { id: 'b', record_type: 'fact' }], edges: [{ from_id: 'a', to_id: 'b', relation: 'SESSION_CONTENT' }, { from_id: 'b', to_id: 'a', relation: 'CAUSES' }] });
t('the memory adapter carries the group and the edge class (a hub edge is not structural, a cause is)', md.nodes[0].group === 'message' && md.edges[0].cls === 'hub' && md.edges[0].structural === false && md.edges[1].structural === true);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
