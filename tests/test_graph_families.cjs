// The unified graph's families (UI redesign, Notes/40 §7; vera/graph/families.js): each adapter maps its fixture to
// nodes and edges with the must-keep fields; merge keeps ids unique; counts; the mixer's off · focus · all; sectors.
//   node tests/test_graph_families.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path');
const F = require(path.join(__dirname, '..', 'vera', 'graph', 'families.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const FIELDS = ['id', 'family', 'kind', 'layer', 'label', 'status', 'weight', 'time', 'real', 'nonAuthoritative', 'group', 'wires', 'parents', 'rec'];
const wellFormed = (d) => d.nodes.every((n) => FIELDS.every((k) => k in n)) && d.edges.every((e) => 'from' in e && 'to' in e && 'label' in e && 'family' in e && 'structural' in e && 'real' in e);

t('seven families in sector order', F.FAMILIES.map((f) => f.id).join(',') === 'turns,context,memory,dag,loop,plan,estate');
const mem = F.toDoc('memory', { nodes: [{ id: 'm1', record_type: 'message', summary: 'hello', importance: 0.9, created_at: '2026-09-11T10:00:00Z', source_type: 'chat' }, { id: 'm2', record_type: 'fact', text: 'x', importance: 0.2, non_authoritative: true }], edges: [{ from_id: 'm1', to_id: 'm2', relation: 'DERIVED_FROM' }, { from: 'm2', to: 'm1', label: 'RELATED', inferred: true }] });
t('memory adapter', mem.nodes.length === 2 && wellFormed(mem) && mem.nodes[0].kind === 'message' && mem.nodes[0].weight === 0.9 && mem.nodes[0].time > 0 && mem.nodes[1].nonAuthoritative && mem.edges[1].real === false && mem.edges[0].label === 'DERIVED_FROM');
const ctx = F.toDoc('context', { nodes: [{ id: 'cap:obs.health', label: 'obs.health', source: 'cap', type: 'capability', score: 1, included: true }, { id: 'vec:1', label: 'a memory', source: 'vector', score: 0.62, included: false }], edges: [{ from: 'vec:1', to: 'cap:obs.health', label: 'RELATED' }], turn: 'm7' });
t('context adapter: retrieved-into the turn for included nodes', ctx.nodes.length === 2 && wellFormed(ctx) && ctx.nodes[1].status === 'excluded' && ctx.edges.some((e) => e.from === 'cap:obs.health' && e.to === 'm7' && e.label === 'RETRIEVED_INTO') && !ctx.edges.some((e) => e.from === 'vec:1' && e.to === 'm7'));
const turns = F.toDoc('turns', { turns: [{ mid: 'm6', role: 'you', text: 'q' }, { mid: 'm7', role: 'aide', text: 'a' }, { mid: 'm8', role: 'you', text: 'ok' }] });
t('turns adapter chains the conversation', turns.nodes.length === 3 && turns.edges.length === 2 && turns.edges[0].label === 'NEXT_IN_SESSION');
const loop = F.toDoc('loop', { events: [{ type: 'start', run_id: 'r1', goal: 'fix boot' }, { type: 'agent_loop_v7.step_start', step_id: 1, title: 'plan' }, { type: 'cap.ok', step_id: 1, tool: 'code.read' }, { type: 'agent_loop_v7.step_done', step_id: 1 }, { type: 'agent_loop_v7.branch_open', step_id: 1, branch: 'b1' }, { type: 'agent_loop_v7.step_start', step_id: 2, parents: ['step:1'], group: 'b1' }, { type: 'agent_loop_v7.assess', step_id: 2, ok: true }, { type: 'agent_loop_v7.step_error', step_id: 2, error: 'x' }, { type: 'done', ok: false }] });
const L = Object.fromEntries(loop.nodes.map((n) => [n.id, n]));
t('loop adapter: run · steps · caps · branches · assess, with status and parents', wellFormed(loop) && L['run:r1'] && L['run:r1'].status === 'fail' && L['step:1'].status === 'ok' && L['cap:step:1:code.read'].status === 'ok' && L['branch:b1'] && L['step:2'].parents[0] === 'step:1' && L['step:2'].group === 'b1' && L['step:2'].status === 'fail' && Object.keys(L).some((k) => /^agent_loop_v7_assess/.test(k)) && loop.edges.some((e) => e.label === 'THEN' && e.from === 'run:r1' && e.to === 'step:1') && loop.edges.some((e) => e.label === 'FORKS'));
t('loop adapter: an inferred record is drawn as such', loop.nodes.filter((n) => n.real === false).length === 1 && loop.edges.some((e) => e.label === 'RECORDS' && !e.structural));
const plan = F.toDoc('plan', { goals: [{ id: 'g1', title: 'ship', status: 'open', children: [{ id: 'g1a', title: 'tests', status: 'done' }] }] });
t('plan adapter', plan.nodes.length === 2 && plan.edges[0].label === 'HAS_MILESTONE' && plan.nodes[1].parents[0] === 'goal:g1');
const est = F.toDoc('estate', { nodes: [{ id: 'ct126', kind: 'host', status: 'up', load: 0.6 }, { name: 'redis', kind: 'service' }], links: [{ source: 'ct126', target: 'redis', kind: 'RUNS' }] });
t('estate adapter', est.nodes.length === 2 && est.nodes[0].id === 'estate:ct126' && est.edges[0].label === 'RUNS' && est.edges[0].to === 'estate:redis');
const dag = F.toDoc('dag', { nodes: [{ id: 'run:1', record_type: 'run' }], edges: [] });
t('dag is memory-shaped and never authoritative', dag.family === 'dag' && dag.nodes[0].family === 'dag' && dag.nodes[0].nonAuthoritative);
t('unknown family says so', F.toDoc('zzz', {}).error === 'unknown family zzz');
const all = F.merge([mem, ctx, turns, loop]);
t('merge keeps ids unique and drops duplicate edges', all.nodes.length === mem.nodes.length + ctx.nodes.length + turns.nodes.length + loop.nodes.length && all.edges.length === mem.edges.length + ctx.edges.length + turns.edges.length + loop.edges.length);
const c = F.counts(all); t('counts per family', c.memory === 2 && c.context === 2 && c.turns === 3 && c.loop === loop.nodes.length && c.estate === 0);
t('merge keeps the first of a duplicated id', F.merge([mem, F.toDoc('memory', { nodes: [{ id: 'm1', record_type: 'other' }] })]).nodes.filter((n) => n.id === 'm1').length === 1);
// the mixer
const doc = F.merge([mem, ctx, turns]);
t('mix: all keeps everything', F.mix(doc, {}, []).nodes.length === doc.nodes.length);
t('mix: off drops a family and its edges', F.mix(doc, { context: 'off' }, []).nodes.every((n) => n.family !== 'context') && F.mix(doc, { context: 'off' }, []).edges.every((e) => e.family !== 'context'));
const foc = F.mix(doc, { context: 'focus', turns: 'focus', memory: 'off' }, ['m7']);
t('mix: focus keeps the focused turn and what touches it, not the rest', foc.nodes.some((n) => n.id === 'cap:obs.health') && !foc.nodes.some((n) => n.id === 'vec:1') && foc.nodes.filter((n) => n.family === 'turns').map((n) => n.id).sort().join(',') === 'm6,m7,m8' && !foc.nodes.some((n) => n.family === 'memory'));
const foc2 = F.mix(doc, { context: 'focus', memory: 'all' }, []);
t('mix: focus without a focus keeps only cross-family touches', foc2.nodes.filter((n) => n.family === 'context').map((n) => n.id).join(',') === 'cap:obs.health' && foc2.nodes.filter((n) => n.family === 'memory').length === 2);
const s = F.sector('memory'); t('sectors are equal bands starting at the top', s[0] < s[1] && Math.abs((s[1] - s[0]) - (Math.PI * 2) / 7) < 1e-9 && F.sector('turns')[0] === -Math.PI / 2);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
