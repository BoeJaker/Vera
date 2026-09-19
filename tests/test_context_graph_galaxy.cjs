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
t('hovering a node opens it', src.indexOf("plot.addEventListener('pointerover'") >= 0 &&
  src.indexOf("closest('.cg-node[data-id],.cg-mem[data-id]')") >= 0);
t('leaving the plot closes it', src.indexOf("plot.addEventListener('pointerleave', () => this._hoverHide());") >= 0);
t('pressing a node closes it, so the record card wins', /pointerdown['"], \(e\) => \{ this\._hoverHide\(\);/.test(src));
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

  const foc = api.compute(Object.assign({}, api.stateFrom(base), { mix: { cap: 'focus' } }), 900, 700);
  t('focus keeps only what went into the prompt', (foc.srcs.find((s) => s.name === 'cap') || {}).n === 2,
    'six records, two of them in the prompt');

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

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
