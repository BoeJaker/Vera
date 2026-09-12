// The exploded scene's layout (UI redesign, the Chat & canvas set's Explode; vera/chat/exploded_element.js): one scene
// per session — a station per turn with what it read, the exchange, what it made, where it landed — projected three
// ways: cards (rows), front (the carousel of the selected station's layers), iso (the lattice through the projection).
//   node tests/test_exploded_element.cjs   (CommonJS: the gate parses js as scripts)
const path = require('node:path'); const fs = require('node:fs');
const X = require(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const scene = { sel: 'm3', turns: [
  { mid: 'm1', who: 'you', t: '14:31', text: 'why is boot so slow lately?', reply: 'Four starts, four full pulls — the fabric re-embeds each time.', read: [{ n: 'obs.provenance · boots', d: '12 rows', col: '#38bdf8', kind: 'table', rows: [{ k: 'boot 1', v: '18.4s' }] }, { n: 'Memory · slow boot', d: 'recalled · 0.71', col: '#5ec9a0', kind: 'memory', score: 0.71 }], made: [{ n: 'Boot timings', d: 'chart · 4 boots', col: '#a78bfa', kind: 'chart' }], land: [{ n: 'Boot log · 4 starts', d: 'table · pinned', col: '#38bdf8', kind: 'canvas' }] },
  { mid: 'm3', who: 'you', t: '14:38', text: 'where exactly does it re-embed?', reply: 'No idempotence check — ensure() pulled and re-embedded on every start.', read: [{ n: 'fabric_capabilities.py', d: '410-486 · 0.94', col: '#a78bfa', kind: 'code', score: 0.94 }], made: [{ n: 'Loop v7 · fixer', d: 'step 4 of 9', col: '#7c9cff', kind: 'loop', steps: [{ label: 'recall', status: 'ok' }, { label: 'author', status: 'run' }] }, { n: '312caef', d: '+7 −2', col: '#fb923c', kind: 'diff', p: 7, m: 2 }], land: [] },
] };
t('five layers in order — the context graph first', X.LAYERS.map((l) => l.key).join(',') === 'graph,read,say,made,land' && X.LAYERS[0].kind === 'graph');
// cards
const c = X.layout(scene, 'cards', 1200, 800);
t('cards: a row per station, the selected one lit', c.stations === 2 && c.plates.length === 2 && c.plates[1].cls === 'on' && c.sel === 1);
t('cards: the exchange is built from the turn and its reply', c.cards.filter((k) => k.layer === 'say' && k.mid === 'm1').length === 2 && c.cards.find((k) => k.layer === 'say' && k.mid === 'm1').card.n === 'why is boot so slow lately?');
t('cards: every card in its column, the columns in layer order', ['read', 'say', 'made', 'land'].every((k, i) => c.cards.filter((x) => x.layer === k).every((x) => x.x === c.cards.find((y) => y.layer === k).x)) && c.cards.find((x) => x.layer === 'read').x < c.cards.find((x) => x.layer === 'say').x && c.cards.find((x) => x.layer === 'say').x < c.cards.find((x) => x.layer === 'made').x);
t('cards: the runs — every read into the exchange, the exchange into every product, a product to what landed', c.edges.filter((e) => e.cls === 'in').length === 3 && c.edges.filter((e) => e.cls === 'out').length === 3 && c.edges.filter((e) => e.cls === 'link').length === 1);
t('cards: rows stack, the scene grows down', c.size.h > 300 && c.plates[1].y > c.plates[0].y + c.plates[0].h);
t('cards: labels — the station (who · time · text) and each layer with its count', c.labels.some((l) => l.cls.indexOf('station') === 0 && /you · 14:31/.test(l.n)) && c.labels.some((l) => /layer read/.test(l.cls) && l.k === '2'));
// front
const f = X.layout(scene, 'front', 1200, 800, { layer: 2 });
t('front: five panels of the selected station, the exchange centred, the others in depth', f.panels.length === 5 && f.panels[2].cls.indexOf('on') === 0 && /translateX\(0px\) translateZ\(0px\) rotateY\(26deg\)/.test(f.panels[2].tf) && /translateZ\(-126px\)/.test(f.panels[1].tf) && f.panels[4].cls.indexOf('far') === 0);
t('front: the selected station\'s cards, four leaders between the five panels', f.panels[1].cards.length === 1 && f.panels[3].cards.length === 2 && f.leaders.length === 4 && f.station.mid === 'm3');
const f2 = X.layout(scene, 'front', 1200, 800, { layer: 3 });
t('front: the focused layer moves the carousel', f2.panels[3].cls.indexOf('on') === 0 && /translateX\(-/.test(f2.panels[2].tf));
// pass B: the context-graph layer, images per tier
const gt = { turns: [{ mid: 'g1', who: 'you', t: '10:00', text: 'why', read: [{ id: 'v1', n: 'fabric.py', kind: 'chunk', score: 0.9 }, { id: 'm1', n: 'recall', kind: 'memory', score: 0.6, included: false }], rel: [{ from: 'm1', to: 'v1', kind: 'mem' }], land: [{ id: 'k1', n: 'boot log', kind: 'note' }], made: [{ n: 'render', kind: 'image', src: 'data:image/png;base64,AAAA' }, { n: 'loop', kind: 'loop', steps: [{ label: 'recall' }, { label: 'author' }] }] }], sel: 'g1' };
const gd = X.graphData(gt.turns[0]);
t('graphData: a lane per family, the members with their relevance, the relations the host recorded, the run\'s steps', gd.laneList.join(',') === 'context,memory,loop,canvas' && gd.nodes.length === 5 && gd.rels.length === 1 && gd.nodes.find((n) => n.id === 'm1').included === false && gd.nodes.filter((n) => n.lane === 'loop').length === 2, JSON.stringify(gd));
const gc = X.layout(gt, 'cards', 1200, 700, { den: 'full' });
t('cards: the graph layer is a graph box, not cards; an image card gets room in Full', gc.graphs.length === 1 && gc.graphs[0].data.nodes.length === 5 && gc.cards.filter((c) => c.layer === 'graph').length === 0 && gc.cards.find((c) => c.card.src).h === 116);
const gh = X.layout(gt, 'cards', 1200, 700, { den: 'hover' });
t('cards: in Hover and Zen the image card keeps its height (the image shows over it)', gh.cards.find((c) => c.card.src).h === 54);
const gi = X.layout(gt, 'iso', 1200, 800, {});
t('iso: the graph band draws the members as nodes on the plate with their relations', gi.gnodes.length === 5 && gi.edges.some((e) => e.cls === 'rel') && gi.cards.filter((c) => c.layer === 'graph').length === 0);
const gf = X.layout(gt, 'front', 1200, 800, {});
t('front: the graph panel carries the graph data', gf.panels[0].layer === 'graph' && gf.panels[0].graph && gf.panels[0].graph.nodes.length === 5);
// iso
const i = X.layout(scene, 'iso', 1200, 800);
t('iso: a plate per station as a four-cornered polygon, the selected lit', i.plates.length === 2 && i.plates.every((p) => p.poly.length === 4) && i.plates[1].cls === 'on');
t('iso: plates are identical parallelograms in a row (same shape, shifted)', (() => { const d = (p) => [p.poly[1].x - p.poly[0].x, p.poly[2].y - p.poly[0].y]; const a = d(i.plates[0]), b = d(i.plates[1]); return Math.abs(a[0] - b[0]) < 0.5 && Math.abs(a[1] - b[1]) < 0.5 && i.plates[1].poly[0].x > i.plates[0].poly[0].x; })());
t('iso: cards anchored at projected points inside the frame, counter-scaled in the DOM', i.cards.length === c.cards.length && i.cards.every((k) => k.anchored && k.x > 0 && k.x < 1200 && k.y > 0 && k.y < 800));
t('iso: the same runs as the cards view', i.edges.filter((e) => e.cls === 'in').length === 3 && i.edges.filter((e) => e.cls === 'out').length === 3 && i.edges.filter((e) => e.cls === 'link').length === 1);
t('iso: the shared projection is used when given', (() => { let n = 0; const P = (u, v, z) => { n++; return [u - v, (u + v) * 0.5 - (z || 0)]; }; X.layout(scene, 'iso', 1200, 800, { proj: P }); return n > 8; })());
t('iso: fitted into the frame', i.fit.s > 0.3 && i.fit.s <= 1.4);
t('an empty session lays out nothing and does not crash', X.layout({ turns: [] }, 'iso', 800, 600).cards.length === 0 && X.layout(null, 'front', 800, 600).panels.length === 0);
t('graphData: the graph sees every record the turn read, the column only the first twelve', X.graphData({ read: [{ id: 'a', n: 'a', score: 1 }], readAll: [{ id: 'a', n: 'a', score: 1 }, { id: 'b', n: 'b', score: 0.5 }], rel: [{ from: 'a', to: 'b', kind: 'cite' }] }).rels.length === 1 && X.graphData({ read: [{ id: 'a', n: 'a', score: 1 }], rel: [{ from: 'a', to: 'b' }] }).rels.length === 0);
t('a placed item wears its template tag (the host hands tpl on the card)', /class="tpl"/.test(fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'), 'utf8')) && /vera:xpl:place/.test(fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'), 'utf8')));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
