// Context ARRIVING in the graph, and the way back to the settings that decide what arrives.
// (vera/chat/context_graph_element.js)
//
// THE ARRIVAL. The legacy context graph got its motion for free: a force layout that re-settled on every fetch,
// so a record joining the turn visibly pushed its way in among the others. The galaxy places deterministically -
// which is what makes it readable, and what made an arrival invisible: the record was simply THERE on the next
// paint, with nothing to catch the eye. drawPlot now marks the records it has not drawn before and they fly out
// of the hub, from the prompt they were assembled for, staggered.
//
// The thing that makes this safe rather than a strobe is that the seen-set is CONSULTED BEFORE IT IS ADDED TO,
// and it belongs to the caller. A paint that changes nothing - a pan, a zoom, a family folded away - finds every
// id already seen and animates nothing. That is the property this test pins, because it is the one that breaks
// silently: an arrival marker that fires on every paint looks fine in a screenshot and is unbearable to use.
//
// THE SETTINGS. Everything the graph shows is context that WAS assembled; the fields that decide what gets
// assembled moved to the host's Settings page, and from the graph there was no way back to them - you could see
// what had been assembled and not what had assembled it. The element owns no settings surface, so its header
// button asks the host, by section.
//
//   node tests/test_context_graph_arrivals.cjs   (CommonJS: the gate parses js as scripts)
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
t('the element loads and exposes drawPlot', !!(api && typeof api.drawPlot === 'function'));
if (!api) { console.log(fails + ' FAILED'); process.exit(1); }

const FAMS = ['memory', 'vector', 'graph', 'fabric', 'cap'];
function build(per, turn) {
  const nodes = [];
  FAMS.forEach((f) => { for (let i = 0; i < per; i++) nodes.push({ id: f + i, label: f + ' record ' + i,
    source: f, text: 'x'.repeat(200), included: true, score: i < 2 ? 0.95 - 0.05 * i : undefined }); });
  const st = api.stateFrom({ nodes, rels: [], view: 'galaxy', color: () => '#888' });
  /* the TURN KEY is what identifies a turn now, not the label - compute() gives the live set the constant 'live'
     and a frame its id, which is exactly why a response streaming in no longer reloads the graph */
  const o = api.compute(st, 900, 700); if (turn) { o.turn = turn; o.turnKey = turn; }
  return o;
}
// records draw as .cg-node and memory RECALLS as .cg-mem - both are context, so both arrive
const arrived = (h) => (h.match(/class="cg-(?:node|mem) [^"]*\barr\b/g) || []).length;
const settled = (h) => (h.match(/class="cg-(?:node|mem) [^"]*\bmov\b/g) || []).length;
// the floor between animations is wall-clock; a test fires its paints back to back, so time is passed by hand
const later = (store) => { store.at = 0; return store; };
const nodesIn = (h) => (h.match(/class="cg-(?:node|mem) /g) || []).length;

// ---- a turn's context flies in, and the same turn painted again does not ------------------------------------
{
  const o = build(6, 'turn-1');
  const store = {};
  const first = api.drawPlot(o, store);
  const n = nodesIn(first);
  t('the first paint of a turn brings every record in', arrived(first) === n && n === 30, 'arrived ' + arrived(first) + ' of ' + n);
  t('a flying record carries the vector back to the hub', /--ax:-?\d+px;--ay:-?\d+px/.test(first));
  t('the cascade is staggered', new Set((first.match(/--ad:(\d+)ms/g) || [])).size > 3, (first.match(/--ad:(\d+)ms/g) || []).slice(0, 4).join(' '));

  // the property that breaks silently: a repaint of the SAME turn must animate nothing
  const again = api.drawPlot(o, store);
  t('repainting the same turn animates nothing', arrived(again) === 0 && nodesIn(again) === n, 'arrived ' + arrived(again));
  const third = api.drawPlot(o, store);
  t('and still nothing on the paint after that', arrived(third) === 0);
}

// ---- one record joining a turn already on screen flies in ALONE ----------------------------------------------
{
  const o = build(6, 'turn-1');
  const store = {};
  api.drawPlot(o, store);
  const o2 = build(7, 'turn-1');          // the same turn, one more record per family
  const h = api.drawPlot(o2, later(store));
  t('a record joining a drawn turn arrives by itself', arrived(h) === 5, 'arrived ' + arrived(h) + ' (expected the 5 new ones)');
}

// ---- a different turn is a new arrival; scrolling back is not a flicker of the old one ------------------------
{
  const o = build(6, 'turn-1'); const store = {};
  api.drawPlot(o, store); api.drawPlot(o, store);
  /* A DIFFERENT TURN IS A MOVE, NOT A LOAD. Scrolling the transcript used to fire the full staggered flight
     for every turn it passed, which is the strobe. The records settle instead: marked, but with no flight
     vector and no stagger. */
  const o2 = build(6, 'turn-2');
  const h2 = api.drawPlot(o2, later(store));
  t('a different turn does not re-fly the whole context', arrived(h2) === 0, 'flew ' + arrived(h2));
  t('it settles instead', settled(h2) === 30, 'settled ' + settled(h2));
  t('a settle carries no flight vector', !/--ax:/.test(h2.slice(h2.indexOf('mov'))) || !/mov[^>]*--ax:/.test(h2));
  t('and the next paint of it animates nothing', arrived(api.drawPlot(o2, later(store))) + settled(api.drawPlot(o2, later(store))) === 0);
}

// ---- no store, no marks: the mini that does not keep one is unaffected ----------------------------------------
{
  const o = build(4, 'turn-1');
  t('a caller with no arrival store gets no marks', arrived(api.drawPlot(o)) === 0);
}

// ---- the stylesheet actually carries the flight, and releases the node's own opacity --------------------------
{
  t('the flight is defined', /@keyframes cg-arrive\b/.test(src));
  t('it starts on the hub and lands on the node', /translate\(calc\(-50% \+ var\(--ax/.test(src));
  const rule = (src.match(/\.cg-node\.arr[^\n]*animation:cg-arrive[^\n}]*/) || [''])[0];
  t('it does not hold its end state, so rank opacity returns', /backwards/.test(rule) && !/\bboth\b|\bforwards\b/.test(rule), rule.slice(0, 120));
  t('reduced motion turns it off', /prefers-reduced-motion:reduce\)\{[\s\S]{0,260}cg-node\.arr[\s\S]{0,60}animation:none/.test(src));
}

// ---- the way back to the settings that decide what arrives ----------------------------------------------------
{
  t('the header offers the context settings', /data-a="settings"/.test(src));
  t('the element asks the host rather than owning a settings surface', /vera:ctx:settings/.test(src));
  t('and names the section it wants', /section: a\.dataset\.sec \|\| 'sources'/.test(src));
}

// ---- the mini's DEFAULT face is the simple galaxy, a different draw - it arrives too --------------------------
{
  const gd = (h) => (h.match(/class="cg-gd [^"]*\barr\b/g) || []).length;
  const nodes = [];
  FAMS.forEach((f) => { for (let i = 0; i < 5; i++) nodes.push({ id: f + i, label: f + ' record ' + i,
    source: f, text: 'x'.repeat(200), included: true, score: 0.9 - i * 0.1 }); });
  const st = api.stateFrom({ nodes, rels: [], view: 'galaxy', color: () => '#888' });
  const o = api.mini(st, 262, 196); o.turn = 'turn-1';
  const store = {};
  const first = api.drawMiniGalaxy(o, 262, 196, store);
  t('the simple galaxy brings its marks in', gd(first) > 0, 'arrived ' + gd(first));
  t('they fly from the galaxy centre, not the plot hub', /--ax:-?\d+px/.test(first));
  t('and the same turn painted again animates nothing', gd(api.drawMiniGalaxy(o, 262, 196, later(store))) === 0);
  /* The two faces SHARE a store, and they do not draw the same marks: the simple galaxy shows each source's
     leading few, the detailed plot shows them all. So flipping face must not replay the marks already seen -
     and must still bring in the records the simple face never drew, because for the viewer those ARE new. */
  const share = {};
  const simpleH = api.drawMiniGalaxy(o, 262, 196, share);
  later(share);
  const drew = (simpleH.match(/class="cg-gd [^"]*" data-id=/g) || []).length;
  const detailed = api.drawPlot(o, share);
  const total = nodesIn(detailed);
  t('flipping face does not replay what was already drawn', arrived(detailed) === total - drew,
    'arrived ' + arrived(detailed) + ', simple had drawn ' + drew + ' of ' + total);
  t('but the records the simple face never drew do arrive', arrived(detailed) > 0 && arrived(detailed) < total);
}

// ---- a burst animates ONCE, not once per paint ---------------------------------------------------------------
{
  /* Context is re-fed several times while a single response comes in, and the transcript scrolls at frame rate.
     Without a floor every one of those paints was its own animation, which is what made the graph look like it
     was reloading over and over. */
  const store = {};
  api.drawPlot(build(6, 'turn-1'), store);              // the load
  const burst = [];
  for (let i = 2; i <= 6; i++) burst.push(arrived(api.drawPlot(build(6, 'turn-' + i), store)) + settled(api.drawPlot(build(6, 'turn-' + i), store)));
  t('five turn changes inside the floor animate nothing further', burst.reduce((a, b) => a + b, 0) === 0, burst.join(','));
  const after = api.drawPlot(build(6, 'turn-9'), later(store));
  t('and once the floor has passed, the next one does', settled(after) > 0, 'settled ' + settled(after));
}

// ---- the turn's identity, not its label -----------------------------------------------------------------------
{
  t('the turn carries a stable key', /out\.turnKey = frame \? String\(frame\.id\) : 'live'/.test(src));
  t('and the arrival is keyed on it', /o\.turnKey != null \? o\.turnKey : \(o\.turn \|\| ''\)/.test(src));
  t('the settle is defined and unstaggered', /@keyframes cg-settle/.test(src) && /\.mov[^\n]*animation:cg-settle/.test(src));
}

// ---- the turns rail reserves a gutter and opens OVER the plot --------------------------------------------------
{
  /* It used to open by growing its own flex-basis, which is a reflow: the plot lost 160px the moment the pointer
     crossed the rail, every node moved, and the drawing jumped under the hand reaching for it - the opposite of
     what its own comment claimed. The flex item is a fixed gutter now and the column inside it is absolute. */
  t('the rail is a fixed gutter', /\.cg-frames\{flex:0 0 26px;width:26px;[^}]*position:relative/.test(src));
  t('it no longer animates its own width in the flow', !/\.cg-frames\{[^}]*transition:[^}]*flex-basis/.test(src));
  t('the column inside it is absolute', /\.cg-fr-in\{position:absolute;left:0;top:0;bottom:0;width:26px/.test(src));
  t('and opening widens THAT, not the flex item', /\.cg-frames:hover \.cg-fr-in,[^{]*\.cg-frames\.wide \.cg-fr-in\{width:190px/.test(src));
  t('the markup wraps the rail parts in it', /'<div class="cg-fr-in"><div class="cg-fr-hd">/.test(src) && /\+ '<\/div><\/div>'; \}/.test(src));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
