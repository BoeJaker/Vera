// The context graph's layout (UI redesign: the Graph board's column; vera/chat/context_graph_element.js): the pure
// compute() places the records by source sector and relevance, hollow when not injected, memory on the outer arc,
// the loop lane and the plan row beside the plot, the relations inside, the record panel; the four views differ.
//   node tests/test_context_graph_element.cjs   (CommonJS: the gate parses js as scripts)
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
  { id: 'm2', label: 'related 1', source: 'memory', score: 0.4, included: false },
];
const edges = [{ from: 'v1', to: 'g1', label: 'CITES' }, { from: 'm2', to: 'v1', label: 'RELATED' }];
const base = { view: 'galaxy', nodes, edges, focus: ['v1', 'g1', 'm1'], reads: { m4: ['v1', 'g1', 'm1'], m2: ['v2'] }, layersOff: new Set(), related: true, loop: [], plan: [], stepReads: [], pan: { x: 0, y: 0, z: 1 } };
const g = G.compute(base, 660, 600);
t('four views', G.VIEWS.map((v) => v[0]).join(',') === 'galaxy,iso,flow,time');
t('galaxy: rings, a spoke and a label per source, in the fixed order', g.rings.length === 3 && g.spokes.length === 4 && g.sectorLabels.map((s) => s.name).join(',') === 'vector,graph,web,cap');
t('context records placed, memory on the arc', g.cnodes.length === 5 && g.memNodes.length === 2 && g.cnodes.every((n) => n.x > 0 && n.y > 0));
const v1 = g.cnodes.find((n) => n.id === 'v1'), v2 = g.cnodes.find((n) => n.id === 'v2'), w1 = g.cnodes.find((n) => n.id === 'w1');
const cx = 330, cy = 300; const dist = (n) => Math.hypot(n.x - cx, n.y - cy);
t('distance is lower relevance', dist(v1) < dist(v2));
t('area is tokens', +v1.d > +v2.d);
t('in the prompt = lit; not injected = hollow; the turn that read it', /\blit\b/.test(v1.cls) && /\bghost\b/.test(w1.cls) && !/\blit\b/.test(v2.cls) && /read by m2/.test(v2.title));
t('a dataset draws square', /\bsq\b/.test(g.cnodes.find((n) => n.id === 'g1').cls));
t('memory: injected on the inner arc, related on the outer, hollow', g.memNodes.find((n) => n.id === 'm1').cls.indexOf('ghost') < 0 && /ghost/.test(g.memNodes.find((n) => n.id === 'm2').cls) && Math.hypot(g.pos.m2.x - cx, g.pos.m2.y - cy) > Math.hypot(g.pos.m1.x - cx, g.pos.m1.y - cy));
t('tokens in the prompt are summed over what is lit', g.tokens === Math.round(800 / 4) + Math.max(12, Math.round('commit 312caef'.length / 4)) + Math.max(12, Math.round('recall 1'.length / 4)) && g.lit === 3);
t('relations inside: lit between lit records, a memory relation dashed', g.cedges.length === 2 && g.cedges.some((e) => e.cls === 'rel lit') && g.cedges.some((e) => e.cls === 'mem'));
t('layers: a chip per source with counts; related count', g.srcs.map((s) => s.name + s.n).join(',') === 'vector2,graph1,web1,cap1' && g.ghosts === 2);
// folding a layer, hiding the related
const g2 = G.compute(Object.assign({}, base, { layersOff: new Set(['vector']), related: false }), 660, 600);
t('a folded layer leaves the plot but stays a chip; related hidden', !g2.cnodes.some((n) => n.id === 'v1') && g2.offSrcs.some((s) => s.name === 'vector') && !g2.cnodes.some((n) => n.id === 'w1') && g2.memNodes.length === 1);
// the views
const iso = G.compute(Object.assign({}, base, { view: 'iso' }), 660, 600), flow = G.compute(Object.assign({}, base, { view: 'flow' }), 660, 600), time = G.compute(Object.assign({}, base, { view: 'time' }), 660, 600);
t('iso: a plate and a stem per record, no rings', iso.plate && iso.stems.length === 5 && iso.rings.length === 0);
t('flow: a column per source, the most relevant on top, hub hidden', flow.cnodes.find((n) => n.id === 'v1').y < flow.cnodes.find((n) => n.id === 'v2').y && flow.hub.hid && flow.spokes.length === 0);
t('time: a column per turn that first read it, never at the far right', time.sectorLabels.some((s) => s.name === 'm4') && time.sectorLabels.some((s) => s.name === 'never') && time.cnodes.find((n) => n.id === 'w1').x > time.cnodes.find((n) => n.id === 'v1').x);
t('the views differ', JSON.stringify(iso.cnodes.map((n) => [n.x, n.y])) !== JSON.stringify(g.cnodes.map((n) => [n.x, n.y])) && JSON.stringify(flow.cnodes.map((n) => [n.x, n.y])) !== JSON.stringify(g.cnodes.map((n) => [n.x, n.y])));
// the loop lane and the plan row, from the loop's events and the goals
const L = G.loopFromEvents([{ type: 'start', run_id: 'r1' }, { type: 'agent_loop_v7.step_start', step_id: 1, title: 'recon' }, { type: 'cap.ok', step_id: 1, tool: 'obs.provenance', wires: ['v1', 'g1'] }, { type: 'agent_loop_v7.step_done', step_id: 1, ms: 1200 }, { type: 'agent_loop_v7.step_start', step_id: 2, title: 'author' }, { type: 'cap', step_id: 2, tool: 'code.author' }]);
t('the loop lane: steps with status, cap and time; what each read', L.steps.length === 2 && L.steps[0].status === 'ok' && L.steps[0].cap === 'obs.provenance' && L.steps[0].ms === '1.2 s' && L.steps[1].status === 'running' && L.stepReads[0].join(',') === 'v1,g1');
const P = G.planFromGoals({ goals: [{ id: 'g1', title: 'find the cause', status: 'done' }, { id: 'g2', title: 'patch the gate', status: 'active' }, { id: 'g3', title: 'test it', status: 'open' }] });
t('the plan row from the goals', P.length === 3 && P[1].label === 'patch the gate');
const g3 = G.compute(Object.assign({}, base, { loop: L.steps, plan: P, stepReads: L.stepReads, allEdges: true }), 660, 600);
t('lanes: the loop down the left, the plan along the top, the plot moved right and down', g3.loopNodes.length === 2 && g3.planNodes.length === 3 && g3.lanes.l === 118 && g3.lanes.t === 52 && g3.loopNodes[1].cls.indexOf('run') === 0 && g3.planNodes[1].cls === 'run' && g3.cnodes.every((n) => +n.x > 118));
t('every step\'s reads run into the plot, dim unless in focus; the plan step in flight hands to the running step', g3.sedges.some((e) => e.cls === 'mem' && /plan step 2/.test(e.title)) && g3.sedges.filter((e) => e.cls === 'rel' && /^step 1 read/.test(e.title)).length === 2 && g3.regions.some((r) => r.t === 'loop · 1 of 2'));
const g4 = G.compute(Object.assign({}, base, { loop: L.steps, plan: P, stepReads: L.stepReads, lsel: 0 }), 660, 600);
t('a loop step in focus draws its reads lit, the others none', g4.sedges.filter((e) => e.cls === 'used' && /^step 1 read/.test(e.title)).length === 2 && g4.loopNodes[0].cls.indexOf('sel') > 0);
// the record panel
const g5 = G.compute(Object.assign({}, base, { sel: 'v1' }), 660, 600);
t('the record: name, kind, relevance in this prompt, read by, relations, the turn to focus', g5.rec && g5.rec.name === 'fabric_capabilities.py' && /in this prompt/.test(g5.rec.rows[0].v) && g5.rec.rows[2].v === 'm4' && g5.rec.rels.length === 2 && g5.rec.turn === 'm4' && /\bon\b/.test(g5.cnodes.find((n) => n.id === 'v1').cls));
t('no records: nothing drawn, no crash', G.compute({ view: 'galaxy', nodes: [], edges: [] }, 300, 200).cnodes.length === 0);
// the chat's other graphs, in this one
const sess = [{ id: 'sm1', record_type: 'message', text: 'why?', created_at: '2026-09-11T10:00:00Z', source_type: 'human', importance: 0.7 }, { id: 'ss1', record_type: 'session', summary: 'session', created_at: '2026-09-11T09:59:00Z' }, { id: 'sd1', record_type: 'dag_step', capability: 'obs.health', created_at: '2026-09-11T10:01:00Z' }, { id: 'v1', record_type: 'fact', text: 'already in the prompt', created_at: '2026-09-11T10:02:00Z' }];
const sessEdges = [{ from_id: 'ss1', to_id: 'sm1', relation: 'SESSION_CONTENT' }, { from_id: 'sm1', to_id: 'sd1', relation: 'FOLLOWED_BY' }, { from_id: 'sd1', to_id: 'v1', relation: 'DERIVED_FROM' }];
const memColor = (ty) => ty === 'message' ? '#5a9e8f' : ty === 'session' ? '#fb923c' : '#a78bfa';
const g6 = G.compute(Object.assign({}, base, { memory: sess, memEdges: sessEdges, memColor, edgeColor: (r) => r === 'FOLLOWED_BY' ? '#8fb87a' : '#888', memHide: new Set(['SESSION_CONTENT']), allEdges: true }), 660, 600);
t('the session memory graph joins the arc, hollow; a record already in the prompt is drawn once, in its sector', g6.memNodes.length === 2 + 3 && g6.memNodes.filter((n) => /ghost/.test(n.cls)).length === 4 && !g6.memNodes.some((n) => n.id === 'v1') && g6.cnodes.some((n) => n.id === 'v1') && g6.pos.sm1.ghost && g6.pos.sm1.sess);
t('the rail\'s shapes and colours: a message is a slab, the session a diamond, a dag step dashed', /\bmsg\b/.test(g6.memNodes.find((n) => n.id === 'sm1').cls) && /\bsess\b/.test(g6.memNodes.find((n) => n.id === 'ss1').cls) && /\bdag\b/.test(g6.memNodes.find((n) => n.id === 'sd1').cls) && g6.memNodes.find((n) => n.id === 'sm1').col === '#5a9e8f');
t('the session\'s relations: drawn among what is on the arc and into the prompt\'s records, hidden types folded', g6.cedges.some((e) => /followed by/i.test(e.title) && e.col === '#8fb87a') && g6.cedges.some((e) => /derived from/i.test(e.title) && /→ fabric_capabilities\.py/.test(e.title)) && !g6.cedges.some((e) => /session content/i.test(e.title)));
t('a memory chip with the count; the region says how many from the session', g6.families.some((f) => f.name === 'memory' && f.n === 5 && f.on) && g6.regions.some((r) => /memory · session 3/.test(r.t)) && g6.ghosts === 2 + 3);
const g7 = G.compute(Object.assign({}, base, { memory: sess, memEdges: sessEdges, layersOff: new Set(['memory']) }), 660, 600);
t('the memory chip folds the session graph away, the prompt\'s own memory stays', g7.memNodes.length === 2 && g7.families.some((f) => f.name === 'memory' && !f.on));
const g8 = G.compute(Object.assign({}, base, { memory: sess, sel: 'sm1' }), 660, 600);
t('the record panel for a session record: kind, recalled, created', g8.rec && g8.rec.family === 'memory' && g8.rec.rows[0].k === 'kind' && /human/.test(g8.rec.rows[0].v) && /never injected/.test(g8.rec.rows[1].v) && g8.rec.rec && g8.rec.rec.id === 'sm1');
// the run's DAG as the loop lane while no loop is live
const dag = [{ id: 'dag:0', label: 'obs.health', status: 'ok', cap: 'health' }, { id: 'dag:1', label: 'code.read', status: 'running', cap: '' }, { id: 'dag:2', label: 'evolve.assess', status: 'pending', cap: '' }];
const g9 = G.compute(Object.assign({}, base, { dag }), 660, 600);
t('the DAG steps take the loop lane, a dag chip, until a loop is live', g9.loopNodes.length === 3 && g9.loopNodes[1].cls.indexOf('run') === 0 && g9.families.some((f) => f.name === 'dag' && f.n === 3) && !g9.families.some((f) => f.name === 'loop'));
const g10 = G.compute(Object.assign({}, base, { dag, loop: L.steps, stepReads: L.stepReads }), 660, 600);
t('a live loop takes the lane over the DAG; a loop chip', g10.loopNodes.length === 2 && g10.families.some((f) => f.name === 'loop') && !g10.families.some((f) => f.name === 'dag'));
t('the loop chip folds the lane away', G.compute(Object.assign({}, base, { dag, layersOff: new Set(['loop']) }), 660, 600).loopNodes.length === 0);
// All edges
const gA = G.compute(Object.assign({}, base, { loop: L.steps, stepReads: L.stepReads }), 660, 600), gB = G.compute(Object.assign({}, base, { loop: L.steps, stepReads: L.stepReads, allEdges: true }), 660, 600);
t('All edges off keeps what touches the prompt; on draws every relation', gA.sedges.filter((e) => e.cls === 'rel').length === 0 && gB.sedges.filter((e) => e.cls === 'rel').length === 2 && gA.cedges.length === gB.cedges.length);
// pass B: the lane and the plan row from the run's own events; the runs picker
const R = G.loopFromEvents([{ type: 'agent_loop_v7.triage_start', goal: 'fix boot' }, { type: 'agent_loop_v7.plan', steps: [{ id: 1, title: 'recon' }, { id: 2, title: 'author' }, { id: 3, title: 'test' }] },
  { type: 'agent_loop_v7.step_start', step_id: 1, title: 'recon' }, { type: 'cap.ok', step_id: 1, tool: 'memory.select', wires: ['v1'] }, { type: 'agent_loop_v7.step_done', step_id: 1, ms: 300 }, { type: 'agent_loop_v6.assess', step_id: 1, ok: true },
  { type: 'agent_loop_v7.step_start', step_id: 2, title: 'author' }, { type: 'agent_loop_v7.subplan', parent_id: 2, steps: [{ id: '2.1', title: 'read' }, { id: '2.2', title: 'edit' }] },
  { type: 'agent_loop_v7.step_start', step_id: '2.1', title: 'read' }, { type: 'agent_loop_v7.step_done', step_id: '2.1' }, { type: 'agent_loop_v7.step_start', step_id: '2.2', title: 'edit' }, { type: 'agent_loop_v6.branch_open', step_id: '2.2', branch: 'b1' },
  { type: 'agent_loop_v7.step_start', step_id: 5, title: 'try b', group: 'b1', parents: ['step:2.2'] }, { type: 'agent_loop_v6.branch_prune', branch: 'b1' }, { type: 'agent_loop_v6.verify', step_id: '2.2', ok: false }]);
t('loopFromEvents: the run, its plan with the steps that ran each, sub-steps under their parent, branches and marks', R.run && R.run.label === 'fix boot' && R.run.status === 'running' && R.plan.map((p) => p.label + ':' + p.status + ':' + p.steps.join('/')).join(',') === 'recon:ok:0,author:running:1/2/3,test:planned:'
  && R.steps.length === 5 && R.steps[2].sub === 1 && R.steps[3].sub === 1 && R.steps[0].sub === -1 && R.steps[4].branch === 'b1' && R.steps[4].pruned === true && R.steps[0].marks[0].kind === 'assess' && R.steps[3].marks[0].kind === 'verify' && R.steps[3].marks[0].status === 'fail', JSON.stringify(R.plan));
const gR = G.compute(Object.assign({}, base, { loop: R.steps, runPlan: R.plan, run: R.run, plan: [{ id: 'goal:g', label: 'a goal', status: 'active' }] }), 660, 600);
t('the run\'s plan takes the row over the goals, EXECUTED_BY drawn to its loop steps, the running one lit', gR.planNodes.length === 3 && gR.planNodes.map((p) => p.cls).join(',') === 'done,run,pend' && gR.sedges.filter((e) => e.cls === 'exec').length === 1 && gR.sedges.filter((e) => e.cls === 'mem' && /plan step 2 ran as loop step/.test(e.title)).length === 3 && gR.regions.some((r) => r.t === 'plan · 3 steps · 1 done'), JSON.stringify(gR.sedges.map((e) => e.cls + ':' + e.title)));
t('the lane: sub-steps indented with a stem, the pruned branch dimmed, marks on the rows', gR.loopNodes[2].x === 22 && gR.loopNodes[0].x === 10 && /\bsub\b/.test(gR.loopNodes[2].cls) && gR.loopStems.length === 2 && /pruned/.test(gR.loopNodes[4].cls) && /\bbr\b/.test(gR.loopNodes[4].cls) && gR.loopNodes[0].marks[0].g === '◔' && gR.loopNodes[3].marks[0].g === '✓' && gR.loopNodes[3].marks[0].status === 'fail');
const gG = G.compute(Object.assign({}, base, { loop: [], runPlan: R.plan, plan: [{ id: 'goal:g', label: 'a goal', status: 'active' }] }), 660, 600);
t('no run in the lane: the goals are the row', gG.planNodes.length === 1 && gG.regions.some((r) => r.t === 'goals · 1 · 0 done'));
const gD = G.compute(Object.assign({}, base, { loop: R.steps, runPlan: R.plan, run: Object.assign({}, R.run, { status: 'fail' }) }), 660, 600);
t('the lane\'s label carries the run\'s status', gD.regions.some((r) => r.t === 'loop · 2 of 5 · failed' && r.col === '#e06c75'));
// the estate strip: the snapshot's leaves under their category, links, "ran on" from the lane; off by default
const SNAP = F.toDoc('estate', { nodes: [{ id: 'hub', label: 'Vera', kind: 'hub', status: 'ok' }, { id: 'cat:nodes', label: 'Nodes', kind: 'category', status: 'warn' }, { id: 'node:ct126', label: 'ct126', kind: 'node', status: 'ok', detail: '61°C', temp_c: 61 }, { id: 'node:ct121', label: 'ct121', kind: 'node', status: 'warn' }, { id: 'cat:docker', label: 'Docker', kind: 'category', status: 'err' }, { id: 'docker:neo4j', label: 'neo4j', kind: 'container', status: 'err' }, { id: 'svc:redis', label: 'Redis', kind: 'service', status: 'ok' }, { id: 'mon:perf', label: 'Perf', kind: 'monitor', status: 'ok' }],
  edges: [{ from: 'hub', to: 'cat:nodes' }, { from: 'cat:nodes', to: 'node:ct126' }, { from: 'cat:nodes', to: 'node:ct121' }, { from: 'hub', to: 'cat:docker' }, { from: 'cat:docker', to: 'docker:neo4j' }, { from: 'hub', to: 'svc:redis' }, { from: 'node:ct126', to: 'docker:neo4j', kind: 'serves' }] });
const RO = G.loopFromEvents([{ type: 'start', run_id: 'r2' }, { type: 'agent_loop_v7.step_start', step_id: 1, title: 'embed' }, { type: 'agent_loop_v5.tool_done', step_id: 1, tool: 'fabric.embed', node: 'ct126' }, { type: 'agent_loop_v7.step_done', step_id: 1 }]);
t('loopFromEvents: the node a step ran on', RO.steps[0].ranOn.join(',') === 'ct126');
const gOff = G.compute(Object.assign({}, base, { estate: { nodes: SNAP.nodes, edges: SNAP.edges }, layersOff: new Set(['estate']) }), 660, 600);
t('off by default: the chip with its count (leaves only — no hub, category or monitor), no strip', gOff.families.some((f) => f.name === 'estate' && f.n === 4 && f.on === false) && gOff.estNodes.length === 0 && gOff.lanes.b === 0);
const gOn = G.compute(Object.assign({}, base, { estate: { nodes: SNAP.nodes, edges: SNAP.edges }, loop: RO.steps, layersOff: new Set() }), 660, 600);
t('on: the strip along the bottom — the leaves grouped under their category, a service in its own group, status classes, the plot above it', gOn.lanes.b === 68 && gOn.estNodes.length === 4 && gOn.estLabels.map((g) => g.t).join(',') === 'Nodes · 2,Docker · 1,Services · 1' && gOn.estNodes.map((n) => n.cls).join(',') === 'ok,warn,err,ok' && gOn.estNodes.every((n) => n.y > 500) && gOn.rings.every((r) => r.cy + r.d / 2 <= 600 - 68 + 1) && gOn.regions.some((r) => r.t === 'estate'), JSON.stringify(gOn.estLabels));
t('the snapshot\'s links and the lane\'s "ran on" are drawn to the strip', gOn.sedges.some((e) => e.cls === 'est' && /ct126 → neo4j · serves/.test(e.title)) && gOn.sedges.some((e) => e.cls === 'ran' && e.title === 'step 1 ran on ct126'));
const gSel = G.compute(Object.assign({}, base, { estate: { nodes: SNAP.nodes, edges: SNAP.edges }, layersOff: new Set(), sel: 'estate:node:ct126' }), 660, 600);
t('a node\'s record: kind, status, detail, temperature', gSel.rec && gSel.rec.family === 'estate' && gSel.rec.rows.map((r) => r.k + '=' + r.v).join(',') === 'kind=node,status=ok,detail=61°C,temperature=61 °C');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
