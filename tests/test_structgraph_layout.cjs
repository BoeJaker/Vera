// The structured graph (<vera-structgraph>, vera/ui/structgraph_element.js; EXPLODE.md §5) lays the contract out as
// bands × columns and routes every run through the shared channel router (vera/ui/routes.js). This checks the
// layout numerically on the two hand-written fixtures — the code slice and the three-paragraph passage — before any
// extractor can hide a problem in it: every leg level or plumb; no two cards overlapping; every card inside its
// plate and every plate inside its parent; no leg through a card; no two legs of different runs sharing a length;
// every edge of the contract a run that starts at its source's side and ends at its target's; crossings bounded;
// the layers toggling cards and edges together; the rank reading left → right; the ports fanned; the gutters widening
// to their demand; the TB direction transposing the ports to top and bottom.
//   node tests/test_structgraph_layout.cjs
const path = require('node:path'); const fs = require('node:fs');
const SG = require(path.join(__dirname, '..', 'vera', 'ui', 'structgraph_element.js')); const RT = require(path.join(__dirname, '..', 'vera', 'ui', 'routes.js'));
const CODE = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'structgraph_code.json'), 'utf8'));
const PROSE = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'structgraph_prose.json'), 'utf8'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const near = (a, b, eps) => Math.abs(a - b) <= (eps == null ? 0.02 : eps);
const endOf = (e) => { const r = e.deg * Math.PI / 180; return { x: e.x + Math.cos(r) * e.len, y: e.y + Math.sin(r) * e.len }; };
const angOf = (e) => ((e.deg % 180) + 180) % 180;
const rect = (k) => ({ x0: k.x, y0: k.y, x1: k.x + k.w, y1: k.y + k.h });
const inside = (a, b, eps) => a.x0 >= b.x0 - (eps || 0) && a.y0 >= b.y0 - (eps || 0) && a.x1 <= b.x1 + (eps || 0) && a.y1 <= b.y1 + (eps || 0);
const overlapR = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
// a segment through a card: the segment's extent, inset by half a pixel, meets the card's rectangle
const through = (e, k) => { const q = endOf(e), r = rect(k), x0 = Math.min(e.x, q.x) + 0.5, x1 = Math.max(e.x, q.x) - 0.5, y0 = Math.min(e.y, q.y) + 0.5, y1 = Math.max(e.y, q.y) - 0.5; return x0 < r.x1 && x1 > r.x0 && y0 < r.y1 && y1 > r.y0; };
// two legs of DIFFERENT runs sharing a length: collinear with overlapping projections (the exploded scene's own check)
const shared = (o) => { const L = o.edges, bad = [];
  for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) { const a = L[i], b = L[j]; if (a.run === b.run) continue; if (!near(angOf(a), angOf(b), 0.6)) continue;
    const r = a.deg * Math.PI / 180, ux = Math.cos(r), uy = Math.sin(r); const perp = Math.abs(-(b.x - a.x) * uy + (b.y - a.y) * ux); if (perp > 0.75) continue;
    const pb0 = (b.x - a.x) * ux + (b.y - a.y) * uy, pb1 = pb0 + (endOf(b).x - b.x) * ux + (endOf(b).y - b.y) * uy;
    const lo = Math.max(0, Math.min(pb0, pb1)), hi = Math.min(a.len, Math.max(pb0, pb1)); if (hi - lo > 1.5) bad.push([a.title, b.title, (hi - lo).toFixed(1)]); }
  return bad; };
// crossings: a level leg and a plumb leg of different runs meeting strictly inside both
const crossings = (o) => { let n = 0; const H = o.edges.filter((e) => near(angOf(e), 0, 0.6)), V = o.edges.filter((e) => near(angOf(e), 90, 0.6));
  H.forEach((h) => { const hx0 = Math.min(h.x, endOf(h).x), hx1 = Math.max(h.x, endOf(h).x); V.forEach((v) => { if (v.run === h.run) return; const vy0 = Math.min(v.y, endOf(v).y), vy1 = Math.max(v.y, endOf(v).y); if (v.x > hx0 + 0.5 && v.x < hx1 - 0.5 && h.y > vy0 + 0.5 && h.y < vy1 - 0.5) n++; }); }); return n; };
const runsOf = (o) => { const m = {}; o.edges.forEach((e) => { (m[e.run] = m[e.run] || []).push(e); }); return m; };
const cardOf = (o, id) => o.cards.find((k) => k.id === id);
const invariants = (name, o, doc) => {
  const cards = o.cards, plates = o.plates;
  t(name + ': every leg of every run is level or plumb', o.edges.length > 0 && o.edges.every((e) => [0, 90, 180, -90].some((d) => near(e.deg, d, 0.01))), JSON.stringify(o.edges.filter((e) => ![0, 90, 180, -90].some((d) => near(e.deg, d, 0.01))).slice(0, 3)));
  t(name + ': no two cards overlap', cards.every((a, i) => cards.every((b, j) => i >= j || !overlapR(rect(a), rect(b)))));
  t(name + ': every card is inside its plate', cards.every((k) => { const p = plates.find((q) => q.id === k.plate); return p && inside(rect(k), rect(p)); }));
  t(name + ': every plate is inside its parent plate, and the plates of one depth never overlap', plates.every((p) => { if (!p.depth) return true; const parent = plates.find((q) => q.depth === p.depth - 1 && inside(rect(p), rect(q))); return !!parent; }) && plates.every((a, i) => plates.every((b, j) => i >= j || a.depth !== b.depth || !overlapR(rect(a), rect(b)))));
  t(name + ': no leg passes through a card', !o.edges.some((e) => cards.some((k) => through(e, k))), JSON.stringify(o.edges.filter((e) => cards.some((k) => through(e, k))).map((e) => [e.title, e.x, e.y, e.len, e.deg]).slice(0, 4)));
  t(name + ': no two legs of different runs share a length', shared(o).length === 0, JSON.stringify(shared(o).slice(0, 4)));
  const R = runsOf(o), E = doc.edges;
  t(name + ': every edge of the contract is a run — ' + E.length, Object.keys(R).length === E.length && o.runs === E.length, Object.keys(R).length + ' runs for ' + E.length + ' edges');
  t(name + ': a run leaves its source at the side and lands on its target', Object.keys(R).every((rid) => { const segs = R[rid]; const from = cardOf(o, segs[0].from), to = cardOf(o, segs[0].to); if (!from || !to) return false;
    const first = segs[0], last = segs[segs.length - 1], q = endOf(last); const onSide = (p, k) => (near(p.x, k.x + k.w + 3, 0.6) || near(p.x, k.x - 3, 0.6)) && p.y >= k.y - 0.5 && p.y <= k.y + k.h + 0.5;
    return onSide({ x: first.x, y: first.y }, from) && onSide(q, to); }));
  t(name + ': every run\'s legs are joined end to end', Object.keys(R).every((rid) => R[rid].every((s, i) => !i || (near(s.x, endOf(R[rid][i - 1]).x, 0.6) && near(s.y, endOf(R[rid][i - 1]).y, 0.6)))));
  t(name + ': the ports of a card are distinct — a bundle leaves and lands fanned', (() => { const by = {}; o.ports.forEach((p) => { (by[p.id + p.side] = by[p.id + p.side] || []).push(p.y); }); return Object.keys(by).every((k) => new Set(by[k].map((y) => y.toFixed(1))).size === by[k].length); })());
  t(name + ': the scene fits its stated size', cards.concat(plates).every((k) => k.x >= 0 && k.y >= 0 && k.x + k.w <= o.size.w + 0.5 && k.y + k.h <= o.size.h + 0.5) && o.edges.every((e) => { const q = endOf(e); return e.x >= 0 && q.x <= o.size.w + 0.5 && e.y >= 0 && q.y <= o.size.h + 0.5; }));
};

// ── the code slice
const c = SG.layout(CODE, 1400, 900);
invariants('code', c, CODE);
t('code: a dependency reads left → right — every exact CALLS / IMPORTS not marked back has its target in a later column', CODE.edges.filter((e) => /CALLS|IMPORTS|INHERITS|CONTAINS/.test(e.kind) && c.back.indexOf(e.from + '>' + e.to) < 0).every((e) => cardOf(c, e.from).col < cardOf(c, e.to).col), JSON.stringify(c.cards.map((k) => [k.id, k.col])));
t('code: the cycle is broken on its back edge and drawn as one', c.back.length === 1 && c.back[0] === 'core.route>nlp._dispatch' && c.edges.some((e) => /\bback\b/.test(e.cls) && e.from === 'core.route'));
t('code: the plates are the files, the class a plate inside its file, the externals a plate of their own', c.plates.filter((p) => p.depth === 0).length === 4 && c.plates.some((p) => p.id === 'c1' && p.depth === 1) && c.plates.some((p) => p.id === 'ext'));
t('code: a heuristic run is dotted, an external one grey, an exact one neither', c.edges.filter((e) => e.from === 'agents.run_stream').every((e) => /\bheur\b/.test(e.cls)) && c.edges.filter((e) => e.to === 'ext.httpx').every((e) => /\bext\b/.test(e.cls)) && c.edges.filter((e) => e.from === 'agents._offload').every((e) => !/heur|ext/.test(e.cls)));
t('code: the kinds are told apart — IMPORTS dashed, INHERITS thick, CALLS plain', c.edges.some((e) => /\bimports dash\b/.test(e.cls)) && c.edges.some((e) => /\binherits thick\b/.test(e.cls)) && c.edges.some((e) => /\bcalls\b/.test(e.cls) && !/dash|thick/.test(e.cls)));
t('code: the busiest gutter widened to its demand — wider than the floor, its lanes on the pitch', (() => { const g = c.geom.gutters; const busiest = Math.max.apply(null, g); return busiest > SG.K.GMIN && g.some((w) => w === SG.K.GMIN); })(), JSON.stringify(c.geom.gutters));
t('code: crossings are few — at most 4 on this slice (2026-09-21: 4 — the back run through the lower channel costs them)', crossings(c) <= 4, 'crossings ' + crossings(c));
t('code: the legend names every kind drawn, with its count', c.legend.map((l) => l.kind).sort().join(',') === Array.from(new Set(CODE.edges.map((e) => e.kind))).sort().join(',') && c.legend.find((l) => l.kind === 'CALLS').n === CODE.edges.filter((e) => e.kind === 'CALLS').length);
t('code: the verdict rail carries the source assessments', c.assessments.length === 2 && c.assessments[0].key === 'syntax');
{ const off = SG.layout(CODE, 1400, 900, { layersOff: { 'code.imports': true } });
  t('code: a layer off takes its edges with it and nothing else', off.runs === CODE.edges.length - 1 && off.cards.length === c.cards.length && !off.edges.some((e) => /imports/.test(e.cls)) && off.layers.find((l) => l.id === 'code.imports').on === false); }
{ const tb = SG.layout(CODE, 900, 1400, { direction: 'TB' });
  t('TB: the same scene transposed — every leg still level or plumb, the ports on top and bottom, the size swapped', tb.edges.every((e) => [0, 90, 180, -90].some((d) => near(e.deg, d, 0.01))) && tb.ports.every((p) => p.side === 'T' || p.side === 'B') && near(tb.size.w, c.size.h) && near(tb.size.h, c.size.w) && !tb.edges.some((e) => tb.cards.some((k) => through(e, k))));
  t('TB: a run leaves its source at the bottom and lands on its target\'s top', (() => { const R = runsOf(tb); return Object.keys(R).every((rid) => { const s = R[rid], from = cardOf(tb, s[0].from), to = cardOf(tb, s[0].to), q = endOf(s[s.length - 1]); return near(s[0].y, from.y + from.h + 3, 0.6) && near(q.y, to.y - 3, 0.6); }); })()); }

// ── the passage
const p = SG.layout(PROSE, 1400, 900);
invariants('prose', p, PROSE);
t('prose · position: the paragraphs are plates top to bottom in their order, the columns the entity types', p.mode === 'position' && p.plates.map((q) => q.id).join(',') === 'p1,p2,p3' && p.plates[0].y < p.plates[1].y && p.plates[1].y < p.plates[2].y && p.columns.join(',') === 'person,org,location,date,event,claim' && cardOf(p, 'e.alice').col === 0 && cardOf(p, 'e.contoso').col === 1);
t('prose · position: an entity stands in the paragraph of its first mention', cardOf(p, 'e.bob').plate === 'p2' && cardOf(p, 'e.close').plate === 'p3' && cardOf(p, 'e.alice').plate === 'p1');
t('prose: two engines over one span are two cards on two layers, joined by a MATCHES run', cardOf(p, 'e.northwind') && cardOf(p, 'e.northwind.spacy') && p.edges.some((e) => /matches dash/.test(e.cls) && e.from === 'e.northwind.spacy'));
{ const off = SG.layout(PROSE, 1400, 900, { layersOff: { 'ner.spacy': true } });
  t('prose: the spaCy layer off — its card and the run touching it gone, GLiNER\'s untouched', !cardOf(off, 'e.northwind.spacy') && off.cards.length === p.cards.length - 1 && off.runs === PROSE.edges.length - 1 && cardOf(off, 'e.northwind')); }
{ const off = SG.layout(PROSE, 1400, 900, { layersOff: { coref: true } });
  t('prose: the coreference layer off — the mention card and its COREF run gone', !cardOf(off, 'e.she') && off.runs === PROSE.edges.length - 1); }
t('prose: co-occurrence is heuristic and dotted; a typed relation is not', p.edges.filter((e) => /cooccurs/.test(e.cls)).every((e) => /\bheur\b/.test(e.cls) && /\bdot\b/.test(e.cls)) && p.edges.filter((e) => e.from === 'e.alice' && e.to === 'e.northwind').every((e) => !/heur/.test(e.cls)));
t('prose: crossings are few — at most 4 on this passage', crossings(p) <= 4, 'crossings ' + crossings(p));
{ const ty = SG.layout(PROSE, 1400, 900, { mode: 'type' });
  invariants('prose · type', ty, PROSE);
  t('prose · type: a band per entity type in the canonical order, the paragraphs as the columns', ty.plates.map((q) => q.id).join(',') === 'kind:person,kind:org,kind:location,kind:date,kind:event,kind:claim' && cardOf(ty, 'e.alice').col === 0 && cardOf(ty, 'e.bob').col === 1 && cardOf(ty, 'e.close').col === 2 && ty.geom.columns === 3); }

// ── verdicts on cards
{ const doc = JSON.parse(JSON.stringify(CODE)); doc.assessments = doc.assessments.concat([
    { key: 'complexity', label: 'complexity', score: 0.4, confidence: 0.9, by: 'cyclomatic', on: 'nlp._dispatch', badge: 'cc 23', evidence: [{ span: { path: 'vera/research/nlp_capabilities.py', start: 800, end: 2100 }, note: 'cyclomatic complexity 23' }] },
    { key: 'tests', label: 'tested', score: 1, confidence: 0.5, by: 'named in tests/', on: 'core.route', badge: 'tested', evidence: [] },
    { key: 'smells', label: 'no smells', score: 0.7, confidence: 0.5, by: 'security patterns', on: 'source', evidence: [{ span: { path: 'vera/research/nlp_capabilities.py', start: 900, end: 910 }, note: 'eval()' }] }]);
  const v = SG.layout(doc, 1400, 900);
  t('a verdict on a card is a badge on it, sized in before layout; a source verdict is not', cardOf(v, 'nlp._dispatch').card.badges.indexOf('cc 23') >= 0 && cardOf(v, 'core.route').card.badges.indexOf('tested') >= 0 && cardOf(v, 'nlp._dispatch').h > cardOf(c, 'nlp._dispatch').h - 1 && !v.cards.some((k) => k.card.badges.indexOf('no smells') >= 0));
  t('the per-card verdicts are kept by card for the element, the source ones on the rail', v.verdicts['nlp._dispatch'].length === 1 && v.assessments.length === 3 && v.assessments.every((a) => !a.on || a.on === 'source'));
  const h = SG.sceneHtml(v);
  t('the rail marks a verdict that carries evidence as clickable', /class="has-ev"[^>]*>no smells/.test(h) && /<i class="warn">cc 23<\/i>/.test(h) && /<i class="ok">tested<\/i>/.test(h)); }

// ── the stage pans, and a chip says what is drawn (owner, 2026-09-22: panning was dead once the scene
//    outgrew the stage, and every chip read the contract's own count whatever was hidden)
{ const SRC = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'structgraph_element.js'), 'utf8');
  t('the drag pans whatever the size — no early return for a scrolling wrap', /pointerdown'[^\n]*closest\('\.sg-card,\.sg-vr,button'\)\) return;/.test(SRC) && !/pointerdown[^\n]*classList\.contains\('scroll'\)/.test(SRC));
  t('a drag that moved is not a click on the card under it', /if \(!drag\.moved && Math\.abs\(dx\) \+ Math\.abs\(dy\) < 3\) return;/.test(SRC) && /this\._dragEnded && Date\.now\(\) - this\._dragEnded < 250\) return;/.test(SRC));
  t('a drag takes over from the scrollbars rather than fighting them', /wrap\.scrollLeft \|\| wrap\.scrollTop\) \{ drag\.px -= wrap\.scrollLeft/.test(SRC));
  t('the wheel zooms about the pointer, shift slides sideways, and a bare embed leaves the page alone', /if \(ev\.shiftKey\) \{ this\._S\.px -= \(ev\.deltaY \|\| ev\.deltaX\)/.test(SRC) && /hasAttribute\('bare'\) && !ev\.ctrlKey\) return;/.test(SRC));
  t('fit shows the whole scene — no half-scale floor stranding a big one', /S\.zoom = Math\.max\(0\.25, z\)/.test(SRC) && !/S\.zoom = Math\.max\(0\.5, z\)/.test(SRC)); }
{ // the counts: what is drawn, and what is hidden by which other layer
  const off = SG.layout(CODE, 1400, 900, { layersOff: { 'code.symbols': false } });
  const rows = (o) => { const m = {}; o.layers.forEach((l) => { m[l.id] = l; }); return m; };
  const on = rows(SG.layout(CODE, 1400, 900));
  t('a chip counts the cards and runs actually drawn, not the contract\'s own number',
    on['code.calls'].drawn === c.edges.filter((e) => /\bcalls\b/.test(e.cls)).map((e) => e.run).filter((v, i, a) => a.indexOf(v) === i).length,
    JSON.stringify({ says: on['code.calls'].drawn }));
  const hid = rows(SG.layout(CODE, 1400, 900, { layersOff: { 'code.symbols': true } }));
  t('hiding the layer that owns the OTHER END of a run takes the run with it, and the chip says so',
    hid['code.calls'].drawn === 0 && hid['code.calls'].hidden > 0 && hid['code.symbols'].on === false,
    JSON.stringify({ drawn: hid['code.calls'].drawn, hidden: hid['code.calls'].hidden, by: hid['code.calls'].hiddenBy }));
  t('and it names the layer that is hiding it', (hid['code.calls'].hiddenBy || []).length > 0, JSON.stringify(hid['code.calls'].hiddenBy));
  t('an off layer offers what it would bring back — its cards AND its runs',
    hid['code.symbols'].hidden === CODE.cards.filter((x) => x.layer === 'code.symbols').length + CODE.edges.filter((x) => x.layer === 'code.symbols').length,
    JSON.stringify({ hidden: hid['code.symbols'].hidden })); }

// ── the shared library
t('routes: rank is longest-path — a chain ranks 0..n, a diamond joins at the far side, a cycle\'s back edge is reported', (() => { const r = RT.rank(['a', 'b', 'c', 'd'], [{ from: 'a', to: 'b' }, { from: 'a', to: 'c' }, { from: 'b', to: 'd' }, { from: 'c', to: 'd' }, { from: 'b', to: 'c' }, { from: 'd', to: 'a' }]); return r.rank.get('a') === 0 && r.rank.get('b') === 1 && r.rank.get('c') === 2 && r.rank.get('d') === 3 && r.depth === 4 && r.back.length === 1 && r.back[0].from === 'd'; })());
t('routes: order lines a cell up with its neighbours', (() => { const y = { a: 10, b: 200, x: 0, y: 0 }; const o = RT.order([['x', 'y']], [{ from: 'x', to: 'b' }, { from: 'y', to: 'a' }], (id) => y[id]); return o[0].join(',') === 'y,x'; })());
t('routes: the exploded scene draws with the same module (VeraRoutes) — cards and iso routers still exported', typeof RT.cardsRouter === 'function' && typeof RT.isoRouter === 'function' && typeof RT.channelRouter === 'function' && RT.version >= 1);
t('the scene renders to markup — a div per plate, card, port and leg', (() => { const h = SG.sceneHtml(c); return (h.match(/class="sg-card /g) || []).length === c.cards.length && (h.match(/class="sg-e /g) || []).length === c.edges.length && (h.match(/class="sg-pl /g) || []).length === c.plates.length && /sg-vr/.test(h); })());
console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);
