// The galaxy: an arm you can read, and a hover that tells you what a node is.
// (vera/chat/context_graph_element.js)
//
// THE ARM. Distance from the hub was a function of SCORE ALONE, through a curve that saturated:
//     r = min(RMAX, 52k + (1 - score)·(RMAX - 52k)/0.4)
// (1 - score)/0.4 reaches 1 at score 0.6, so every record scoring 0.6 or less sat on RMAX exactly — and a record
// with no score defaults to 0.5, which is most of them (memory, fabric, caps, skills and ontologies carry no
// relevance number). Measured over a five-family plot, nine records each: only THREE distinct radii per family
// out of nine, the other six stacked on one ring of touching circles. That is "nodes of the same type stacked
// one atop the other".
//
// Rank now breaks the tie — the family's list is already sorted by score, so rank preserves the ordering and only
// separates what score cannot. Half the arm each, chosen by measurement: at 24 records per family, overlapping
// node pairs fall 76 → 48 and the closest pair improves 14.8 → 15.5px, while the nine-per-family case holds.
// Leaning harder on rank (0.8, 0.92) removes a few more overlaps but drops the closest pair to 7.6px.
//
// And the angle advances with the radius, which is what makes a spiral instead of the spokes-and-rings radar it
// drew before: there was no spiral term anywhere in the layout.
//
// THE HOVER. A node carried a native title= tooltip and nothing else. The record card is a click, and far too
// heavy to stand in for a look. Verified live on a real grown graph of 48 nodes: hovering an ontology node
// showed "vera_system_ontology" / "ontology 0.80 12 tok in the prompt", and leaving hid it again.
//
//   node tests/test_context_graph_galaxy.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const SRC = path.join(__dirname, '..', 'vera', 'chat', 'context_graph_element.js');
const src = fs.readFileSync(SRC, 'utf8');

// load the element with just enough of a browser for it to define itself
function load() {
  const el = () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, appendChild() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, addEventListener() {}, dataset: {}, textContent: '' });
  const doc = { createElement: el, createElementNS: el, head: el(), body: el(), getElementById() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, adoptedStyleSheets: [] };
  const win = { document: doc, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }), requestAnimationFrame: () => 0,
    CSSStyleSheet: function () { this.replaceSync = () => {}; },
    customElements: { define() {}, get() {} }, HTMLElement: function () {}, CustomEvent: function () {} };
  win.window = win; vm.runInContext(src, vm.createContext(win)); return win.VeraContextGraph;
}
const api = load();
t('the element loads and exposes compute', !!(api && typeof api.compute === 'function'));
if (!api) { console.log(fails + ' FAILED'); process.exit(1); }

// five families; two records carry a real score, the rest carry NONE — the ordinary shape
const FAMS = ['memory', 'vector', 'graph', 'fabric', 'cap'];
function plot(per) {
  const nodes = [];
  FAMS.forEach((f) => { for (let i = 0; i < per; i++) nodes.push({ id: f + i, label: f + ' record ' + i,
    source: f, text: 'x'.repeat(200), included: true, score: i < 2 ? 0.95 - 0.05 * i : undefined }); });
  const st = api.stateFrom({ nodes, rels: [], view: 'galaxy', color: () => '#888' });
  const o = api.compute(st, 900, 700);
  const hub = { x: parseFloat(o.hub.x), y: parseFloat(o.hub.y) };
  const rows = Object.keys(o.pos).map((id) => { const p = o.pos[id];
    return { id, p, r: Math.hypot(p.x - hub.x, p.y - hub.y), a: Math.atan2(p.y - hub.y, p.x - hub.x) * 180 / Math.PI }; });
  return { o, hub, rows };
}

// ---- the tie is broken: every record in a family gets its own radius -----------------------------------------
{
  const { rows } = plot(9);
  FAMS.forEach((f) => {
    const rs = rows.filter((x) => x.p.source === f).map((x) => +x.r.toFixed(1));
    if (f === 'memory') return;                       // the memory arm is placed by its own ring, not the arm
    t('every ' + f + ' record has its own radius', new Set(rs).size === rs.length,
      new Set(rs).size + ' distinct of ' + rs.length + ' — unscored records are stacking again');
  });
}

// ---- the radius is not saturated: unscored records spread, they do not all sit on the rim ---------------------
{
  const { rows, o } = plot(9);
  const vec = rows.filter((x) => x.p.source === 'vector').map((x) => x.r).sort((a, b) => a - b);
  const span = vec[vec.length - 1] - vec[0];
  t('a family spans a real length of arm', span > 120, 'span ' + span.toFixed(0) + 'px');
  // the ARM families only: memory is drawn on its own ring outside the context, one radius by design, and is
  // spread by angle instead — so it is checked that way rather than excused
  const arm = rows.filter((x) => x.p.source !== 'memory');
  const rim = Math.max(...arm.map((x) => x.r));
  const onRim = arm.filter((x) => Math.abs(x.r - rim) < 0.5).length;
  t('the rim is not a pile-up', onRim <= FAMS.length, onRim + ' records share the outermost radius');

  const mem = rows.filter((x) => x.p.source === 'memory').sort((a, b) => a.a - b.a);
  if (mem.length > 1) {
    let gap = 1e9;
    for (let i = 1; i < mem.length; i++) gap = Math.min(gap, Math.hypot(mem[i].p.x - mem[i - 1].p.x, mem[i].p.y - mem[i - 1].p.y));
    t('the memory ring separates its records by angle', gap > 18, 'closest neighbours ' + gap.toFixed(1) + 'px');
  }
}

// ---- the arm curves: angle advances with radius, which is the spiral -----------------------------------------
{
  const { rows } = plot(9);
  const vec = rows.filter((x) => x.p.source === 'vector').sort((a, b) => a.r - b.r);
  const unwrap = (d) => ((d % 360) + 540) % 360 - 180;
  const turn = unwrap(vec[vec.length - 1].a - vec[0].a);
  t('the arm turns as it leaves the hub', Math.abs(turn) > 8,
    'turn ' + turn.toFixed(1) + '° — with no swirl this is a straight spoke, not an arm');
}

// ---- families stay apart, and nothing is worse than it was ---------------------------------------------------
{
  const { rows } = plot(9);
  let cross = 1e9, same = 1e9, overlap = 0;
  for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
    const A = rows[i], B = rows[j], d = Math.hypot(A.p.x - B.p.x, A.p.y - B.p.y);
    if (A.p.source === B.p.source) same = Math.min(same, d); else cross = Math.min(cross, d);
    if (d < (A.p.rim + B.p.rim)) overlap++;
  }
  t('no two records overlap at nine per family', overlap === 0, overlap + ' overlapping pairs');
  t('families are clearly apart', cross > 40, 'closest cross-family ' + cross.toFixed(1) + 'px');
  t('records within a family stay legible', same > 24, 'closest same-family ' + same.toFixed(1) + 'px');
}

// ---- the hover card ------------------------------------------------------------------------------------------
t('the plot carries a hover slot', src.indexOf('<div class="cg-hover" data-r="hover" hidden></div>') >= 0);
// ONE implementation, used by BOTH graphs: the mini is static HTML the chat drops into its rail, so the
// behaviour is exported rather than written twice. Two tooltips for one gesture is what this replaces.
t('the hover is a shared binder, not wiring on one plot', src.indexOf('function bindHover(root, getRec, opts) {') >= 0);
t('...exported, so the chat can put the same card on the mini', /const api = \{[^}]*bindHover, hoverCard/.test(src));
t('the expanded plot uses it', src.indexOf('this._hoverHide = bindHover(plot, (id) => this._last && this._last.pos[id])') >= 0);
t('hovering a node opens it', src.indexOf("root.addEventListener('pointerover'") >= 0 &&
  src.indexOf("opts.sel || '.cg-node[data-id],.cg-mem[data-id],.cg-gd[data-id]'") >= 0,
  'the mini\'s DEFAULT face is the simple galaxy, whose marks are .cg-gd — leaving it out means the hover works '
  + 'on the expanded graph and on nothing the reader usually looks at');
// and no face keeps the browser's own tooltip, or two would show at once
t('the simple face drops its native title too', !/cg-gd[\s\S]{0,400}title="' \+ esc\(row\.label/.test(src));
t('leaving closes it', src.indexOf("root.addEventListener('pointerleave', hide);") >= 0);
t('pressing closes it, so the record card wins', src.indexOf("root.addEventListener('pointerdown', hide);") >= 0);
t('binding twice does not stack listeners', src.indexOf('if (!root || root._cgHoverBound) return; root._cgHoverBound = true;') >= 0);
// and the browser's own tooltip is gone from the record nodes, or both would show at once
t('no native title on a record node', !/class="cg-node ' \+ n\.cls \+ '" data-id="[^"]*" \+ esc\(n\.id\)[^;]*title=/.test(src)
  && src.indexOf('data-kind="\' + esc(n.kind || \'\') + \'" title="\' + esc(n.title)') < 0);
t('it is NOT the record card', src.indexOf('function hoverCard(p) {') >= 0 &&
  src.indexOf('hoverCard') >= 0 && !/function hoverCard[\s\S]{0,900}data-a="zoom"/.test(src),
  'the hover card must not carry the click card\'s buttons');
t('it never eats the pointer', /\.cg-hover\{[^}]*pointer-events:none/.test(src),
  'a card under the cursor would flicker as the pointer left the node');
t('it says what the record is', /cg-hv-t/.test(src) && /cg-hv-m/.test(src) && /in the prompt/.test(src));

// ---- the list under the graph shows the whole record ---------------------------------------------------------
// A row showed 140 characters and nothing else, so the only way to learn what a record was, or which side of the
// turn read it, was to click through to the card. Under the graph the list is where records are READ.
{
  const LONG = 'The narrator keeps a journal of every step the loop took, and the reader needs the whole of it, '
    + 'not the first twenty words, because the interesting part is usually at the end. '.repeat(3);
  const st = api.stateFrom({ nodes: [
    { id: 'm1', label: 'A memory', source: 'memory', text: LONG, score: 0.82, included: true, type: 'fact' },
    { id: 'w1', label: 'A page', source: 'web', text: 'short', score: 0.7, included: false, url: 'https://example.com/p' },
  ], rels: [], view: 'galaxy', color: () => '#8fb87a' });
  const o = api.compute(st, 900, 700);
  const full = api.listHtml(o, {}), compact = api.listHtml(o, { compact: true });
  t('a row carries a detail block', full.indexOf('cg-row-d') >= 0);
  t('it names the family and the relevance', />memory</.test(full) && /relevance 0\.82/.test(full));
  t('it says whether the record went into the prompt', full.indexOf('in the prompt') >= 0 && full.indexOf('>excluded<') >= 0);
  const body = (full.match(/cg-row-x">([^<]*)/) || [])[1] || '';
  t('the body is the record, not a 140-character tease', body.length > 300, body.length + ' chars');
  t('a url is reachable from the row', full.indexOf('cg-row-u') >= 0);
  t('the rail\'s compact face is left alone', compact.indexOf('cg-row-d') < 0 && compact.indexOf('<small>') >= 0);
}

// ---- the meter is the section control, and the legend ---------------------------------------------------------
// The sources were a row of chips carrying a colour, a name and a count — all three of which the meter carries
// while also showing the one thing they could not: how much of the window each source spends.
{
  const mk = (s, n, inc) => Array.from({ length: n }, (_, i) => ({ id: s + i, label: s + ' ' + i, source: s,
    text: 'y'.repeat(300), included: inc === undefined ? true : i < inc, score: 0.7 }));
  const base = { nodes: [...mk('cap', 6, 2), ...mk('web', 2)], rels: [], view: 'galaxy', color: () => '#8fb87a' };
  const all = api.compute(api.stateFrom(base), 900, 700);
  const cap = all.srcs.find((s) => s.name === 'cap') || {};
  t('a source carries its level and its token weight', cap.level === 'all' && cap.tok > 0 && cap.n === 6);

  /* FOCUS IS THE FEW THAT MATTER. It used to mean "only the records that went into the prompt", which is a
     no-op in ordinary use — almost everything assembled has included !== false — so focus drew what all drew and
     the control read as a plain on/off. It is the top three by relevance now, as focus means everywhere else in
     this graph. Ten records, every one of them included: */
  const ten = { nodes: Array.from({ length: 10 }, (_, i) => ({ id: 'c' + i, label: 'cap ' + i, source: 'cap',
    text: 'x'.repeat(100), included: true, score: 0.95 - i * 0.05 })), rels: [], view: 'galaxy', color: () => '#888' };
  const focAll = api.compute(api.stateFrom(ten), 900, 700);
  const focFew = api.compute(Object.assign({}, api.stateFrom(ten), { mix: { cap: 'focus' } }), 900, 700);
  t('all draws the whole family', (focAll.srcs.find((s) => s.name === 'cap') || {}).n === 10);
  t('focus draws the few that matter, not the same ten', (focFew.srcs.find((s) => s.name === 'cap') || {}).n === 3,
    'focus must differ from all, or the three levels are two');

  const off = api.compute(Object.assign({}, api.stateFrom(base), { mix: { cap: 'off' } }), 900, 700);
  t('off drops the source from the plot', !off.srcs.find((s) => s.name === 'cap'));
  t('...but it keeps its place on the meter, at its own width',
    (off.offSrcs.find((s) => s.name === 'cap') || {}).tok > 0,
    'a band that vanished could never be pressed again');
}
t('the meter replaces the source chips', src.indexOf('class="cg-meter"') >= 0 && src.indexOf('class="cg-mb ') >= 0);
t('a band is sized by its share of the window', /flex-grow:' \+ Math\.max\(1, Math\.round\(\(s\.tok \|\| 0\) \/ meterTot \* 1000\)\)/.test(src));
t('pressing a source cycles off, focus, all — the same three a family has',
  /k === 'layer'[\s\S]{0,220}cur === 'off' \? 'focus' : cur === 'focus' \? 'all' : 'off'/.test(src));
t('and the old two-state source toggle is gone', src.indexOf("if (S.layersOff.has(s)) S.layersOff.delete(s); else S.layersOff.add(s);") < 0);
t('an off band is still visible enough to press back on', /\.cg-mb\.off\{background:color-mix\(in srgb,var\(--mc\) 16%/.test(src));

// ---- a search must not leave relations to records it faded out -----------------------------------------------
// The search dims what does not match rather than removing it, but the relations were drawn from the node set
// alone and took no notice — so searching left full-strength edges running to records that had just faded, and
// the graph appeared to relate things that were not there. Measured before: 2 of 4 nodes drawn, all 3 relations
// still at full strength, two of them into the dimmed pair.
{
  const nodes = [
    { id: 'a1', label: 'alpha one', source: 'vector', text: 'alpha', included: true, score: 0.9 },
    { id: 'a2', label: 'alpha two', source: 'vector', text: 'alpha', included: true, score: 0.8 },
    { id: 'b1', label: 'beta one', source: 'cap', text: 'beta', included: true, score: 0.7 },
    { id: 'b2', label: 'beta two', source: 'cap', text: 'beta', included: true, score: 0.6 },
  ];
  const rels = [{ from: 'a1', to: 'b1', label: 'CITES' }, { from: 'a2', to: 'b2', label: 'SIMILAR' }, { from: 'a1', to: 'a2', label: 'RELATED' }];
  const at = (extra) => api.compute(Object.assign({}, api.stateFrom({ nodes, rels, view: 'galaxy', color: () => '#888' }), extra), 900, 700);
  const bright = (o) => o.cedges.filter((e) => /\brel\b/.test(e.cls) && !/\bmiss\b/.test(e.cls));

  const plain = at({});
  t('with no search every relation is drawn', bright(plain).length === 3);

  const q = at({ q: 'alpha' });
  const drawn = new Set(q.cnodes.filter((n) => !/\bmiss\b/.test(n.cls)).map((n) => n.id));
  t('the search dims the records that do not match', drawn.size === 2, [...drawn].join(','));
  t('and no relation into a faded record stays bright', bright(q).length === 1,
    bright(q).map((e) => e.title).join(' | ') + ' — an edge must be no brighter than its dimmest end');

  // the spokes to the chat answer to it too: a spoke to a faded record is the same lie, pointing at the hub
  const sp = at({ q: 'alpha', allEdges: true });
  const spokes = sp.cedges.filter((e) => /\bspoke\b/.test(e.cls));
  t('a spoke to a faded record fades with it', spokes.some((e) => /\bmiss\b/.test(e.cls)),
    spokes.length + ' spokes, none dimmed');
}

// ---- the relation legend is a meter, coded to the edges -------------------------------------------------------
{
  const nodes = [{ id: 'x', label: 'x', source: 'vector', text: 'x', included: true, score: 0.9 },
    { id: 'y', label: 'y', source: 'vector', text: 'y', included: true, score: 0.8 }];
  const o = api.compute(api.stateFrom({ nodes, rels: [{ from: 'x', to: 'y', label: 'CITES' }], view: 'galaxy', color: () => '#888' }), 900, 700);
  t('a relation type carries a colour of its own', !!(o.edgeTypes[0] && o.edgeTypes[0].col),
    'a grey legend cannot say which lines are which type');
  // and DIFFERENT types get DIFFERENT colours. Deferring to the host's edgeColor collapsed the whole legend to
  // one value — measured live as six bands sharing a single colour — because it answers the same default for
  // every context type; it exists for the memory graph's styling, not for this.
  {
    const many = { nodes: [{ id: 'p', label: 'p', source: 'vector', text: 'p', included: true, score: 0.9 },
      { id: 'q', label: 'q', source: 'vector', text: 'q', included: true, score: 0.8 },
      { id: 'r', label: 'r', source: 'cap', text: 'r', included: true, score: 0.7 }],
      rels: [{ from: 'p', to: 'q', label: 'CITES' }, { from: 'q', to: 'r', label: 'HAS_SKILL' }, { from: 'p', to: 'r', label: 'DEFINES' }],
      view: 'galaxy', color: () => '#888', edgeColor: () => '#999' };   // a host that answers one colour for all
    const om = api.compute(api.stateFrom(many), 900, 700);
    const cols = new Set(om.edgeTypes.map((t) => t.col));
    t('...and three types are three colours, whatever the host says', cols.size === 3, [...cols].join(','));
    // the EDGES are drawn in it, or the legend codes to nothing
    const relCols = new Set(om.cedges.filter((e) => /\brel\b/.test(e.cls)).map((e) => e.col || ''));
    t('the relations themselves are drawn in their type colour', relCols.size >= 2, [...relCols].join(','));
  }
  t('the same type is the same colour every render',
    api.compute(api.stateFrom({ nodes, rels: [{ from: 'x', to: 'y', label: 'CITES' }], view: 'galaxy', color: () => '#888' }), 900, 700).edgeTypes[0].col === o.edgeTypes[0].col);
}
t('the relations are drawn as a meter, not a chip row', /class="cg-meter et"/.test(src) && src.indexOf('cg-lay et ') < 0);
t('a band is sized by how many relations it holds', /flex-grow:' \+ Math\.max\(1, Math\.round\(t\.n \/ tot \* 1000\)\)/.test(src));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
