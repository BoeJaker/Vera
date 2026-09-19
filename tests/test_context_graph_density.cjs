// How crowded the expanded context graph actually is, measured rather than eyeballed.
// (vera/chat/context_graph_element.js)
//
// The question that started this was the right one: "is it scaling the elements to fit the new size, or did you
// not fix that?" Neither. Node diameter never scaled with the plot - and RMAX never grew when the panel was
// widened, because RMAX is bounded by the SHORTER side and only the width had changed. Widening the panel from
// 660 to 932px moved the radius not one pixel; all it bought was the elliptical stretch, and that was computed
// from a rule of thumb (aspect x 0.92) that left the drawing spanning 72% of the width it had been given.
//
// Three things were actually wrong, and each was measured before and after:
//
//   1. SECTORS WERE EQUAL. step = CTXW / srcs.length, so a family holding twelve capabilities got the same arc
//      as one holding two. That is where most of the crowding lived, and it is why a bigger panel barely helped.
//   2. THE STRETCH DID NOT FILL THE BOX. Now chosen so the whole drawing - RMAX plus the margin the memory ring
//      and the sector labels are laid out in - spans the width. Dividing by RMAX alone overshoots by that margin
//      times the stretch and pushes the memory arm out of the plot; the exploded sheet's containment test caught
//      exactly that, which is why it is checked here too.
//   3. THE MARKS WERE FAT. 18.2px marks with an 8px gap between them. 15px is the floor that still draws the
//      kind icon (14px), and it is worth more than either of the other two on its own.
//
// Measured over a 56-record turn at the grown graph's real 932x650, against what shipped:
//      overlapping pairs 10 -> 0 | pairs under 6px 22 -> 2 | worst gap -2.9px -> +5.7px | median 8.2 -> 11.7
// and over the 24-records-in-one-family case that was the known-bad one:
//      overlapping 22 -> 0 | worst -11.1px -> +11.0px | median -10.3px -> +20.3px
//
//   node tests/test_context_graph_density.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const SRC = path.join(__dirname, '..', 'vera', 'chat', 'context_graph_element.js');
const src = fs.readFileSync(SRC, 'utf8');
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
t('the element loads', !!(api && typeof api.compute === 'function'));
if (!api) { console.log(fails + ' FAILED'); process.exit(1); }

// gaps measured RIM TO RIM, which is what the eye reads - centre distance flatters a fat mark
function gaps(nodes, W, H) {
  const st = api.stateFrom({ nodes, rels: [], view: 'galaxy', color: () => '#888' });
  const o = api.compute(st, W, H);
  const pts = o.cnodes.map((n) => ({ x: +n.x, y: +n.y, d: +n.d }))
    .concat(o.memNodes.map((n) => ({ x: +n.x, y: +n.y, d: 9 })));
  const nn = pts.map((p, i) => {
    let best = Infinity;
    pts.forEach((q, j) => { if (i === j) return;
      const g = Math.hypot(p.x - q.x, p.y - q.y) - (p.d + q.d) / 2; if (g < best) best = g; });
    return best; });
  nn.sort((a, b) => a - b);
  const xs = pts.map((p) => p.x);
  return { n: pts.length, d: pts[0].d, min: nn[0], p10: nn[Math.floor(nn.length * 0.1)],
    median: nn[Math.floor(nn.length / 2)], tight: nn.filter((g) => g < 6).length,
    over: nn.filter((g) => g <= 0).length,
    usedW: (Math.max.apply(null, xs) - Math.min.apply(null, xs)) / W };
}
const mixed = (() => { const ns = [];
  [['vector', 9], ['graph', 7], ['fabric', 7], ['cap', 12], ['skill', 7], ['ontology', 7], ['memory', 7]]
    .forEach(([f, per]) => { for (let i = 0; i < per; i++) ns.push({ id: f + i, label: f + ' record ' + i, source: f,
      text: 'x'.repeat(300), included: true, score: i < 2 ? 0.92 - 0.06 * i : undefined }); }); return ns; })();
const lopsided = (() => { const ns = [];
  [['cap', 24], ['vector', 3], ['graph', 2], ['fabric', 2], ['skill', 2], ['ontology', 2], ['memory', 2]]
    .forEach(([f, per]) => { for (let i = 0; i < per; i++) ns.push({ id: f + i, label: f + ' record ' + i, source: f,
      text: 'x'.repeat(300), included: true, score: i < 2 ? 0.92 - 0.06 * i : undefined }); }); return ns; })();

// ---- nothing overlaps at the size the grown graph actually is ------------------------------------------------
{
  const g = gaps(mixed, 932, 650);
  t('no two records overlap', g.over === 0, JSON.stringify(g));
  t('the worst pair is clear of its neighbour', g.min >= 4, 'worst ' + g.min.toFixed(1) + 'px');
  t('almost nothing is crowded (was 22 of 56)', g.tight <= 6, g.tight + ' pairs under 6px');
  t('the median gap is not swallowed by the mark', g.median >= 10, 'median ' + g.median.toFixed(1) + 'px');
}
// ---- the one-huge-family case, which used to be a pile ---------------------------------------------------------
{
  const g = gaps(lopsided, 932, 650);
  t('24 records in one family no longer overlap at all', g.over === 0, JSON.stringify(g));
  t('and they are properly apart, not merely separate', g.min >= 8, 'worst ' + g.min.toFixed(1) + 'px');
}
// ---- the drawing uses the room it is given ---------------------------------------------------------------------
{
  t('the galaxy spans the width it has (was 72%)', gaps(mixed, 932, 650).usedW >= 0.75, (gaps(mixed, 932, 650).usedW * 100).toFixed(0) + '%');
  // a tall narrow column must be left alone - the stretch is never below 1
  const tall = gaps(mixed, 420, 900);
  t('a tall narrow column is not stretched into a letterbox', tall.usedW <= 0.95, (tall.usedW * 100).toFixed(0) + '%');
}
// ---- and the whole drawing still FITS, margin included -----------------------------------------------------------
{
  /* the stretch must scale RMAX + memMargin, not RMAX: the memory ring and the sector labels are laid out in
     that margin and the stretch carries them out with it. Small boxes are where the error shows. */
  const fits = (W, H) => { const st = api.stateFrom({ nodes: mixed, rels: [], view: 'galaxy', color: () => '#888' });
    const o = api.mini(Object.assign({}, st, { view: 'galaxy' }), W, H);
    return [].concat(o.cnodes || [], o.memNodes || []).every((n) => {
      const d = +(n.d || 12); return +n.x - d / 2 >= 0 && +n.y - d / 2 >= 0 && +n.x + d / 2 <= W && +n.y + d / 2 <= H; }); };
  t('everything fits a small sheet', fits(183, 121));
  t('everything fits the rail mini', fits(262, 196));
  t('everything fits the grown plot', fits(932, 650));
  t('the stretch divides by the whole drawing, not by RMAX', /Math\.min\(2\.0, \(\(PW - LANE_L\) \/ 2\) \/ Math\.max\(1, RMAX \+ memMargin\)\)/.test(src));
}
// ---- the sectors are sized by what they hold ---------------------------------------------------------------------
{
  t('sector width is proportional to the records in it', /const SECTOR_W = 1(\.0)?,/.test(src) && /famCnt\.map\(\(c\) => Math\.pow\(Math\.max\(1, c\), SECTOR_W\)\)/.test(src));
  t('with a floor so a one-record family is still readable', /Math\.max\(even \* SECTOR_FLOOR/.test(src));
  t('and normalised back inside the arc the context owns', /w\.map\(\(x\) => x \* CTXW \/ sum\)/.test(src));
  t('the mark is 15px, the floor that still draws its kind icon', /const d = Math\.max\(15 \* k/.test(src) && /n\.d >= 14 \? iconSvg/.test(src));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
