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
const g3 = G.compute(Object.assign({}, base, { loop: L.steps, plan: P, stepReads: L.stepReads }), 660, 600);
t('lanes: the loop down the left, the plan along the top, the plot moved right and down', g3.loopNodes.length === 2 && g3.planNodes.length === 3 && g3.lanes.l === 118 && g3.lanes.t === 52 && g3.loopNodes[1].cls.indexOf('run') === 0 && g3.planNodes[1].cls === 'run' && g3.cnodes.every((n) => +n.x > 118));
t('every step\'s reads run into the plot, dim unless in focus; the plan step in flight hands to the running step', g3.sedges.some((e) => e.cls === 'mem' && /plan step 2/.test(e.title)) && g3.sedges.filter((e) => e.cls === 'rel' && /^step 1 read/.test(e.title)).length === 2 && g3.regions.some((r) => r.t === 'loop · 1 of 2'));
const g4 = G.compute(Object.assign({}, base, { loop: L.steps, plan: P, stepReads: L.stepReads, lsel: 0 }), 660, 600);
t('a loop step in focus draws its reads lit, the others none', g4.sedges.filter((e) => e.cls === 'used' && /^step 1 read/.test(e.title)).length === 2 && g4.loopNodes[0].cls.indexOf('sel') > 0);
// the record panel
const g5 = G.compute(Object.assign({}, base, { sel: 'v1' }), 660, 600);
t('the record: name, kind, relevance in this prompt, read by, relations, the turn to focus', g5.rec && g5.rec.name === 'fabric_capabilities.py' && /in this prompt/.test(g5.rec.rows[0].v) && g5.rec.rows[2].v === 'm4' && g5.rec.rels.length === 2 && g5.rec.turn === 'm4' && /\bon\b/.test(g5.cnodes.find((n) => n.id === 'v1').cls));
t('no records: nothing drawn, no crash', G.compute({ view: 'galaxy', nodes: [], edges: [] }, 300, 200).cnodes.length === 0);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
