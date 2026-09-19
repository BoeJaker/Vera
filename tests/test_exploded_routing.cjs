// The exploded scene's RUNS are routed, not drawn as chords (UI redesign, the Canvas board's rule: "edges live BEHIND the
// cards and route down a gutter, never diagonally"; in iso "every leg changes exactly one of u, v, z"). This checks the two
// routers numerically — every leg orthogonal in cards, every leg on the lattice in iso, no two legs of different runs
// sharing a length, every canvas widget joined to something — and the two optional layers (activity, estate) and the
// galaxy option that the same layout carries.
//   node tests/test_exploded_routing.cjs
const path = require('node:path'); const fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'); const X = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const scene = { sel: 'm2', turns: [
  { mid: 'm1', who: 'you', t: '14:31', text: 'why is boot so slow lately?', reply: 'Four starts, four full pulls.',
    read: [{ id: 'v1', n: 'fabric_capabilities.py', d: 'vector · 0.94', col: '#a78bfa', kind: 'chunk', score: .94 }, { id: 'mm', n: 'boot memo', d: 'memory · 0.71', col: '#5ec9a0', kind: 'memory', score: .71 }, { id: 'pg', n: 'issue #41', d: 'web · 0.55', kind: 'page', score: .55 }, { id: 'c1', n: 'obs.provenance', d: 'cap · 0.5', kind: 'cap', score: .5 }],
    rel: [{ from: 'mm', to: 'v1', kind: 'mem' }, { from: 'pg', to: 'v1', kind: 'cite' }], say: [],
    made: [{ n: 'obs.provenance', d: 'capability · done', col: '#8fb87a', kind: 'cap', body: 'branch bleeding-edge', key: 'turn:m1:cap:0' }, { n: 'Boot timings', d: 'widget · bars', col: '#a78bfa', kind: 'widget', form: 'bars', data: { a: 18, b: 17 }, key: 'turn:m1:widget:1' }, { n: 'a diff', d: 'diff', kind: 'diff', p: 4, m: 1 }],
    land: [{ n: 'Boot timings', d: 'canvas · lifted', col: '#a78bfa', kind: 'widget', form: 'bars', key: 'turn:m1:widget:1' }, { n: 'GPU', d: 'canvas · placed from the registry', col: '#8fb87a', kind: 'widget', tpl: 'thermo', form: 'thermo', key: 'widget:thermo:m1' }],
    activity: [{ cap: 'obs.provenance', n: 'obs.provenance', t: '14:31', ms: 120, status: 'ok' }, { cap: 'fabric.status', n: 'fabric.status', t: '14:31', ms: 80, status: 'ok' }, { cap: 'code.author', n: 'code.author', t: '14:32', ms: 4100, status: 'error' }],
    estate: [{ id: 'cat:fabric', label: 'Fabric', kind: 'category', status: 'ok', acts: [1] }, { id: 'node:ct118', label: 'ct118', kind: 'host', status: 'warn', via: 0, acts: [] }] },
  { mid: 'm2', who: 'you', t: '14:38', text: 'where exactly does it re-embed?', reply: 'ensure() pulled and re-embedded on every start.',
    read: [{ id: 'v1', n: 'fabric_capabilities.py', d: 'vector · 0.94', col: '#a78bfa', kind: 'chunk', score: .94 }, { id: 'lg', n: 'vera_start.log', d: 'vector · 0.8', kind: 'chunk', score: .8 }],
    say: [], made: [{ n: 'python · 12 lines', d: 'code block', kind: 'code', body: 'def ensure(): pass' }], land: [] }] };
const legs = (o, pred) => o.edges.filter((e) => !/prompt/.test(e.cls) && (!pred || pred(e)));
const near = (a, b, eps) => Math.abs(a - b) <= (eps == null ? 0.02 : eps);
const angOf = (e) => ((e.deg % 180) + 180) % 180;
const endOf = (e) => { const r = e.deg * Math.PI / 180; return { x: e.x + Math.cos(r) * e.len, y: e.y + Math.sin(r) * e.len }; };
// two legs of DIFFERENT runs sharing a length: collinear (same angle, no perpendicular gap) with projections that overlap
const overlaps = (o) => { const L = legs(o); const bad = [];
  for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) { const a = L[i], b = L[j]; if (a.run === b.run) continue; if (!near(angOf(a), angOf(b), 0.6)) continue;
    const r = a.deg * Math.PI / 180, ux = Math.cos(r), uy = Math.sin(r); const perp = Math.abs(-(b.x - a.x) * uy + (b.y - a.y) * ux); if (perp > 0.75) continue;
    const pa0 = 0, pa1 = a.len, pb0 = (b.x - a.x) * ux + (b.y - a.y) * uy, pb1 = pb0 + (endOf(b).x - b.x) * ux + (endOf(b).y - b.y) * uy;
    const lo = Math.max(Math.min(pa0, pa1), Math.min(pb0, pb1)), hi = Math.min(Math.max(pa0, pa1), Math.max(pb0, pb1)); if (hi - lo > 1.5) bad.push([a.title, b.title, (hi - lo).toFixed(1)]); }
  return bad; };
const runsOf = (o, cls) => new Set(o.edges.filter((e) => e.cls === cls).map((e) => e.run)).size;
const endsOn = (o, box, eps) => o.edges.some((e) => { const q = endOf(e); const hit = (p) => Math.abs(p.x - box.x) <= box.w / 2 + (eps || 6) && Math.abs(p.y - box.y) <= box.h / 2 + (eps || 6); return hit(q) || hit({ x: e.x, y: e.y }); });

// ── CARDS ─────────────────────────────────────────────────────────────────────────────────────────────────────────
const c = X.layout(scene, 'cards', 1400, 900);
t('cards: every leg of every run is level or plumb', legs(c).every((e) => [0, 90, 180, -90].some((d) => near(Math.abs(e.deg), Math.abs(d), 0.01))));
t('cards: no two legs of different runs share a length', overlaps(c).length === 0, JSON.stringify(overlaps(c).slice(0, 4)));
t('cards: the runs are the board\'s — every read into the exchange, the exchange into every product, a product to what landed, a relation per record pair',
  runsOf(c, 'in') >= 6 && runsOf(c, 'in mem') === 1 && runsOf(c, 'out') === 4 && runsOf(c, 'link') === 1 && runsOf(c, 'rel mem') === 1 && runsOf(c, 'rel cite') === 1, JSON.stringify({ in: runsOf(c, 'in'), mem: runsOf(c, 'in mem'), out: runsOf(c, 'out'), link: runsOf(c, 'link'), rm: runsOf(c, 'rel mem'), rc: runsOf(c, 'rel cite') }));
{ const gpu = c.cards.find((k) => k.layer === 'land' && k.card.n === 'GPU'), bt = c.cards.find((k) => k.layer === 'land' && k.card.n === 'Boot timings');
  const box = (k) => ({ x: k.x + k.w / 2, y: k.y + k.h / 2, w: k.w, h: k.h });
  t('cards: a widget lifted out of the reply is joined to the product it came from, by key', runsOf(c, 'link') === 1 && endsOn(c, box(bt)));
  t('cards: a widget placed by hand — nothing produced it — is joined to the exchange it was placed on (dashed)', runsOf(c, 'link dash') === 1 && endsOn(c, box(gpu)) && c.edges.some((e) => e.cls === 'link dash' && /placed on this turn/.test(e.title)));
  t('cards: a widget on the canvas plane can be picked up (it says so to the renderer) and carries its key', gpu.drag === true && gpu.key === 'widget:thermo:m1' && c.cards.filter((k) => k.layer !== 'land').every((k) => !k.drag)); }
{ // the ports: runs leaving one card fan its side, never leaving at one point
  const ex = c.cards.find((k) => k.layer === 'say' && k.mid === 'm1'); const outs = legs(c, (e) => e.cls === 'out' && Math.abs(e.x - (ex.x + ex.w + 3)) < 1); const ys = new Set(outs.map((e) => e.y));
  t('cards: the runs leaving the exchange leave by different ports (' + ys.size + ' of ' + outs.length + ')', outs.length >= 3 && ys.size === outs.length); }
t('cards: the galaxy sheet is an option — off, the board\'s lanes alone; on, the sheet past the lanes', c.graphs.length === 0 && X.layout(scene, 'cards', 1400, 900, { galaxy: true }).graphs.length === 2);
{ const a = X.layout(scene, 'cards', 1400, 900, { layers: { activity: true } });
  t('cards + activity: the turn\'s calls are typed nodes in the produced plate, in order', a.anodes.length === 3 && a.anodes.every((n) => n.mid === 'm1' && n.lane === 'activity' && n.icon === X.ICON.cap) && a.anodes[0].x < a.anodes[1].x && a.anodes.every((n) => n.x > a.plates[3].x && n.x < a.plates[3].x + a.plates[3].w));
  t('cards + activity: a call hangs off the capability card that ran it, the others off the exchange; the chain runs call to call; a failed call is red', runsOf(a, 'act') === 3 && runsOf(a, 'rel step act') === 2 && a.edges.some((e) => e.cls === 'act' && /run by the card/.test(e.title)) && a.anodes[2].col === 'var(--xp-red)' && a.anodes[0].col === 'var(--xp-ac2)');
  t('cards + activity: the produced plate grew for the lane; the layout is still orthogonal and unshared', a.plates[3].h > c.plates[3].h && legs(a).every((e) => [0, 90, 180, -90].some((d) => near(Math.abs(e.deg), Math.abs(d), 0.01))) && overlaps(a).length === 0, JSON.stringify(overlaps(a).slice(0, 3)));
  t('cards: without the layer the scene is as it was — no activity nodes, no sixth plate', c.anodes.length === 0 && c.enodes.length === 0 && c.plates.length === 10); }
{ const e = X.layout(scene, 'cards', 1400, 900, { layers: { activity: true, estate: true } });
  t('cards + estate: a sixth plate per turn, the estate nodes in it, the plates still fitting the width', e.plates.length === 12 && e.plates.filter((p) => p.layer === 'estate').length === 2 && e.enodes.length === 2 && e.enodes.every((n) => n.x > e.plates[5].x && n.x < e.plates[5].x + e.plates[5].w) && e.geom.kW <= 1);
  t('cards + estate: a call runs to the subsystem it ran through; the machine behind it is joined dashed; a warning is amber', runsOf(e, 'est') === 1 && runsOf(e, 'rel est dash') === 1 && e.enodes[1].col === 'var(--xp-dv2)' && e.enodes[0].icon === X.ICON.service && e.enodes[1].icon === X.ICON.host);
  t('cards + estate: still orthogonal, still unshared', legs(e).every((x) => [0, 90, 180, -90].some((d) => near(Math.abs(x.deg), Math.abs(d), 0.01))) && overlaps(e).length === 0, JSON.stringify(overlaps(e).slice(0, 3))); }

// ── ISO ───────────────────────────────────────────────────────────────────────────────────────────────────────────
const i = X.layout(scene, 'iso', 1400, 900);
// the lattice's three screen directions under the classic projection (tilt 30, azimuth 45): u, v, and straight up
const lattice = (o) => { const T = (o.tilt || 30) * Math.PI / 180, A = (o.azim || 45) * Math.PI / 180; const sT = Math.sin(T), cA = Math.cos(A), sA = Math.sin(A);
  const ang = (x, y) => ((Math.atan2(y, x) * 180 / Math.PI % 180) + 180) % 180; return [ang(cA, sA * sT), ang(-sA, cA * sT), 90]; };
const onLattice = (o) => { const L = lattice(o); return legs(o).every((e) => L.some((d) => near(angOf(e), d, 0.6) || near(Math.abs(angOf(e) - d), 180, 0.6))); };
const offLattice = (o) => { const L = lattice(o); return legs(o).filter((e) => !L.some((d) => near(angOf(e), d, 0.6) || near(Math.abs(angOf(e) - d), 180, 0.6))).map((e) => [e.cls, e.deg]); };
t('iso: every leg of every run lies along the lattice — along u, along v or straight up — never a chord', onLattice(i), JSON.stringify(offLattice(i).slice(0, 5)));
t('iso: no two legs of different runs share a length', overlaps(i).length === 0, JSON.stringify(overlaps(i).slice(0, 4)));
t('iso: the runs are the board\'s, counted by run not by leg', runsOf(i, 'in') >= 6 && runsOf(i, 'in mem') === 1 && runsOf(i, 'out') === 4 && runsOf(i, 'link') === 1 && runsOf(i, 'link dash') === 1 && runsOf(i, 'rel mem') === 1 && runsOf(i, 'rel cite') === 1, JSON.stringify({ in: runsOf(i, 'in'), mem: runsOf(i, 'in mem'), out: runsOf(i, 'out'), link: runsOf(i, 'link'), ld: runsOf(i, 'link dash') }));
t('iso: a relation inside the graph band is an L along the lattice, and still names the records it joins', i.edges.filter((e) => e.cls === 'rel mem').every((e) => e.joins && e.joins[0] === 'mm' && e.joins[1] === 'v1'));
{ const gpu = i.widgets.find((w) => w.layer === 'land' && w.card.n === 'GPU'), bt = i.widgets.find((w) => w.layer === 'land' && w.card.n === 'Boot timings');
  t('iso: every canvas widget is joined at its grounding pin — the lifted one to its product, the hand-placed one to the exchange', !!gpu && !!bt && endsOn(i, { x: gpu.x, y: gpu.y, w: 2, h: 2 }, 3) && endsOn(i, { x: bt.x, y: bt.y, w: 2, h: 2 }, 3));
  t('iso: the canvas widgets can be picked up; the others cannot', gpu.drag && bt.drag && gpu.key === 'widget:thermo:m1' && i.widgets.filter((w) => w.layer !== 'land').every((w) => !w.drag)); }
{ const pw = (o) => Math.abs(o.plates[0].poly[1].x - o.plates[0].poly[0].x); const small = X.layout({ sel: 'm2', turns: [scene.turns[1]] }, 'iso', 1400, 900);
  t('iso: the plate\'s margins grow with the lanes the runs need (the board: the inset grows with the relations)', pw(i) > pw(small)); }
t('iso: the galaxy sheet is an option — off by default, the lanes alone; on, the sheet lies past the nodes', i.graphs.length === 0 && (() => { const g = X.layout(scene, 'iso', 1400, 900, { galaxy: true }); return g.graphs.length === 2 && g.graphs.every((s) => s.iso); })());
{ const a = X.layout(scene, 'iso', 1400, 900, { layers: { activity: true, estate: true } });
  t('iso + layers: the calls chain along the produced band, the estate is a sixth band on every plate', a.anodes.length === 3 && a.bands.filter((b) => b.layer === 'estate').length === 2 && a.enodes.length === 2 && a.labels.some((l) => /activity/.test(l.cls) && /3 calls/.test(l.k)));
  t('iso + layers: a call to the card that ran it, call to call, call to the subsystem, machine to service — all on the lattice, none shared', runsOf(a, 'act') === 3 && runsOf(a, 'rel step act') === 2 && runsOf(a, 'est') === 1 && runsOf(a, 'rel est dash') === 1 && onLattice(a) && overlaps(a).length === 0, JSON.stringify(offLattice(a).slice(0, 3)) + ' ' + JSON.stringify(overlaps(a).slice(0, 3)));
  t('iso + layers: the same under a tilt and a swing', (() => { const b = X.layout(scene, 'iso', 1400, 900, { layers: { activity: true, estate: true }, tilt: 18, azim: 60, stack: true }); return onLattice(b) && overlaps(b).length === 0; })()); }
t('the layout reports what the chip bar needs: the selected turn\'s call and estate counts', X.layout(scene, 'iso', 1400, 900).ctx.acts === 0 && X.layout(Object.assign({}, scene, { sel: 'm1' }), 'iso', 1400, 900).ctx.acts === 3 && X.layout(Object.assign({}, scene, { sel: 'm1' }), 'iso', 1400, 900).ctx.ests === 2);

// ── the element: the switches, the pick-up, the edit, the faces ────────────────────────────────────────────────
t('the chip bar carries the two layers and the galaxy as switches, remembered per browser', /data-layer="activity"/.test(SRC) && /data-layer="estate"/.test(SRC) && /data-galaxy="1"/.test(SRC) && /readPref\('vera_xpl_layers'/.test(SRC) && /writePref\('vera_xpl_galaxy'/.test(SRC) && /layers\(v\) \{/.test(SRC) && /galaxy\(on\) \{/.test(SRC) && /new CustomEvent\('vera:xpl:layers'/.test(SRC));
t('a widget on the canvas plane can be picked up and dropped — the plate lights, the drop is reported, the click that follows is swallowed', /this\.addEventListener\('pointerdown', \(e\) => \{ if \(e\.button\) return; const it = e\.target\.closest && e\.target\.closest\('\[data-drag\]'\)/.test(SRC) && /new CustomEvent\('vera:xpl:move'/.test(SRC) && /if \(this\._dragJust\) \{ this\._dragJust = false; return; \}/.test(SRC) && /\.xp-pl\.drop\{/.test(SRC) && /data-drag="1" data-mid="/.test(SRC) && /if \(this\._dragW && this\._dragW\.on\) \{ this\._renderHeld = true; return; \}/.test(SRC) && /\.xp-wrap\.dropping \.xp-pl\{pointer-events:auto\}/.test(SRC));
t('and edited: a gear on the card reports vera:xpl:edit with the record', /class="xit-edit" data-edit="1"/.test(SRC) && /new CustomEvent\('vera:xpl:edit'/.test(SRC) && /_itemOf\(id\) \{/.test(SRC));
t('a widget waiting for its first reading is not dimmed — it is marked, not faded', !/\.xig\.sample\{opacity:\.55\}/.test(SRC) && !/\.xit-face\.sample\{opacity:\.82\}/.test(SRC) && /\.xit-face\.sample::after\{content:'sample'/.test(SRC) && /\.xig\.sample\{opacity:1\}/.test(SRC));
t('the two layers\' nodes are drawn like the context nodes, typed and titled', /class="xnd ' \+ \(n\.lane === 'activity' \? 'act' : 'est'\)/.test(SRC) && /vera-exploded \.xnd\.act\{/.test(SRC) && /vera-exploded \.xnd\.est\{/.test(SRC));
t('the routers are the module\'s (node-testable) and the API says so', typeof X.cardsRouter === 'function' && typeof X.isoRouter === 'function' && typeof X.landRuns === 'function' && X.version >= 10);

console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
