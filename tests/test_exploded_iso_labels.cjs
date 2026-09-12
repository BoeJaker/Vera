// The exploded scene's iso drawn the way the Canvas board draws it (Canvas.dc.html): every item the board's CARD (.xit —
// name · meta · a body by kind) standing on a stem at its pin; a widget an ISO WIDGET GROUP (.xig, built through the ISO
// lib's box/face/scene) with a frameless caption; a context record a typed ICON NODE (.xnd) about the prompt line, the
// galaxy sheet past them; and the items scale WITH the scene under the zoom — their counter-scale follows the fit alone.
//   node tests/test_exploded_iso_labels.cjs
const path = require('node:path'); const fs = require('node:fs'); const vm = require('node:vm');
const FILE = path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'); const X = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
// the shared projection, as the page serves it (/ui/iso.js): loaded into a stand-in window
const W = {}; vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'iso.js'), 'utf8'), { window: W }); const ISO = W.VeraISO;
let f = 0; const ok = (c, m, extra) => { console.log((c ? 'ok   ' : 'FAIL ') + m + (c ? '' : '  ' + (extra || ''))); if (!c) f++; };
const t = { mid: 'm1', who: 'you', t: '14:31', text: 'why is boot slow?', reply: 'Four starts, four full pulls.',
  read: [{ id: 'v1', n: 'fabric_capabilities.py', d: 'vector · 0.94', col: '#a78bfa', kind: 'chunk', score: .94, body: 'corpus = pull_all()' }, { id: 'mm', n: 'boot memo', d: 'memory · 0.71', col: '#5ec9a0', kind: 'memory', score: .71, included: false }, { id: 'pg', n: 'issue #41', d: 'web · 0.55', kind: 'page', score: .55 }],
  rel: [{ from: 'mm', to: 'v1', kind: 'mem' }, { from: 'pg', to: 'v1', kind: 'cite' }], say: [],
  made: [{ n: 'obs.provenance', d: 'capability · done', col: '#8fb87a', kind: 'cap', body: 'branch bleeding-edge\nreembeds 4' }, { n: 'Boot timings', d: 'widget · bars', col: '#a78bfa', kind: 'widget', form: 'bars', data: { a: 18, b: 17, c: 19 } }, { n: '312caef', d: '+7 −2', col: '#fb923c', kind: 'diff', p: 7, m: 2 }, { n: 'Agentic loop', d: 'loop · 5 steps', kind: 'loop', steps: [{ label: 'recall', status: 'ok' }, { label: 'read', status: 'ok' }, { label: 'probe', status: 'run' }, { label: 'author' }, { label: 'test' }] }],
  land: [{ n: 'GPU', d: 'canvas · placed from the registry', col: '#8fb87a', kind: 'widget', tpl: 'thermo', form: 'thermo' }, { n: 'Boot table', d: 'canvas · table', kind: 'table', rows: [{ k: 'boot 1', v: '18s' }, { k: 'boot 2', v: '17s' }, { k: 'boot 3', v: '19s' }, { k: 'boot 4', v: '18s' }] }] };
const scene = { turns: [t, { mid: 'm2', who: 'you', t: '14:38', text: 'where?', read: [], say: [], made: [], land: [] }], sel: 'm1' };
const o = X.layout(scene, 'iso', 1200, 800, {});
// (1) every item is the board's card at its pin — a widget an iso widget group with its caption
ok(o.widgets.length === 12 && o.widgets.every((w) => w.cw === 200 && w.ch === 54 && w.stem === 14), 'every item carries the board\'s card (200 × 54, a 14px stem) at its pin: 3 read · 2 exchange · 4 made · 2 landed · turn 2\'s question (' + o.widgets.length + ')');
const groups = o.widgets.filter((w) => w.draw === 'group'), cards = o.widgets.filter((w) => w.draw === 'card');
ok(groups.length === 2 && groups.every((w) => w.card.kind === 'widget'), 'the reply widget and the placed widget are iso widget groups; nothing else is (' + groups.length + ')');
ok(cards.length === 10 && cards.some((w) => w.card.kind === 'cap') && cards.some((w) => w.card.kind === 'diff') && cards.some((w) => w.card.kind === 'chunk'), 'records, capabilities, diffs, loops and tables are cards');
const bt = groups.find((w) => w.card.n === 'Boot timings'); ok(bt && bt.form === 'bars' && bt.value === 'c 19', 'a reading widget\'s caption carries its value (the top of the set)');
const gpu = groups.find((w) => w.card.tpl); ok(gpu && gpu.placed && gpu.sample && gpu.value === '', 'a placed widget draws as its form with sample data, no reading in its caption yet');
ok(X.layout(scene, 'iso', 1200, 800, { stack: true }).widgets.every((w) => w.cw === 172 && w.ch === 47 && w.tight), 'Stack: the cards tighten (172 × 47, the body behind hover / click)');
// the widget group, built through the ISO lib about the pin
const dial = X.groupOf({ form: 'bar', data: { value: 62, max: 100 }, w: 78, col: '#8fb87a', value: '62%' }, ISO);
ok(dial && dial.kind === 'dial' && dial.faces.length === 19 * 3 && dial.needle && /deg$/.test(dial.needle.deg) && dial.big && dial.big.n === '62%', 'a level draws as a dial: 18 studs and a hub as faces, a needle, the big value');
const bars = X.groupOf(bt, ISO); ok(bars && bars.kind === 'bars' && bars.faces.length === 4 * 3 && bars.faces.every((q) => /^polygon\(/.test(q.cp) && /px$/.test(q.x)), 'a set draws as bars on a slab: a box per value, three faces each, clip-paths in px');
const blk = X.groupOf({ form: 'string', data: 'x', w: 78, col: '#fff' }, ISO); ok(blk && blk.kind === 'block' && blk.faces.length === 6 && blk.sh > 0 && blk.bh > 0, 'anything else is a block; the group is lifted by half its height to clear the caption');
ok(X.groupOf(bt, null) === null, 'without the lib there is no group (the element draws the flat widget card)');
ok(X.groupOf({ form: 'bars', data: { a: 1 }, w: 108 }, ISO).bw > X.groupOf({ form: 'bars', data: { a: 1 }, w: 60 }, ISO).bw, 'Size S · M · L scales the widget');
// the card bodies, the board's vocabulary
const body = (c) => X.isoBody(c, X.widgetOf(c));
ok(/xf-score/.test(body(t.read[0]).on) && /xf-bar/.test(body(t.read[0]).on) && /<pre>corpus/.test(body(t.read[0]).x), 'a read record: the score bar on the card, its text behind a click');
ok(/xf-term/.test(body(t.made[0]).on) && /branch bleeding-edge/.test(body(t.made[0]).on) && /<pre>/.test(body(t.made[0]).x), 'a capability: the first line as a terminal line, the rest behind a click');
ok(/xf-diff/.test(body(t.made[2]).on) && /\+7/.test(body(t.made[2]).on) && /−2/.test(body(t.made[2]).on), 'a diff: +added −removed');
ok((body(t.made[3]).on.match(/xf-ls/g) || []).length === 3 && (body(t.made[3]).x.match(/xf-ls/g) || []).length === 2 && /xf-ls run/.test(body(t.made[3]).on), 'a loop: three steps on the card, the rest behind a click, the running one lit');
ok((body(t.land[1]).on.match(/xf-tr/g) || []).length === 3 && (body(t.land[1]).x.match(/xf-tr/g) || []).length === 1, 'a table: three rows on the card, the rest behind a click');
ok(/xf-w/.test(body(t.made[1]).on) && /c 19/.test(body(t.made[1]).on) && /xf-ws/.test(body(t.made[1]).on), 'a widget without the lib: the board\'s widget card — the reading and a sparkline');
ok(/xf-code/.test(body({ kind: 'code', body: 'def x():\n  pass' }).on), 'a code block: a line of code');
// (1b) the context records are typed icon nodes about the prompt line; the galaxy lies past them
ok(o.gnodes.length === 10 && o.gnodes.every((n) => n.mid === 'm1' && n.d >= 19 && n.d <= 28 && /^M/.test(n.icon)), 'the graph band draws every record the turn read, what landed and the loop\'s steps as icon nodes (' + o.gnodes.length + ')');
const nv = o.gnodes.find((n) => n.nid === 'v1'), nm = o.gnodes.find((n) => n.nid === 'mm'), np = o.gnodes.find((n) => n.nid === 'pg');
ok(nv.icon === X.ICON.file && nm.icon === X.ICON.memory && np.icon === X.ICON.page && o.gnodes.find((n) => n.nid === 'GPU').icon === X.ICON.canvas, 'the icon is the record\'s kind: a chunk a file, a memory the memory glyph, a page a page, a canvas item the canvas');
ok(nv.lit && !nv.ghost && nm.ghost && !nm.lit && nv.lane === 'context' && nm.lane === 'memory', 'lit when it is in the prompt, hollow when it only relates; a lane per family');
ok(o.edges.filter((e) => e.cls === 'rel').length === 2 && o.edges.some((e) => /prompt/.test(e.cls)), 'the relations the host recorded run between the nodes; the prompt line runs down the band');
ok(o.labels.some((l) => /lane/.test(l.cls) && l.n === 'memory' && l.k === '1') && o.labels.some((l) => /prompt/.test(l.cls)), 'the lanes are labelled at the plate\'s edge; the prompt line is named');
ok(o.graphs.length === 1 && o.graphs[0].iso && o.graphs[0].y > Math.max.apply(null, o.gnodes.map((n) => n.y)), 'the galaxy sheet still lies on the plate, past the nodes');
const sameShape = (a, b) => Math.abs((a[1].x - a[0].x) - (b[1].x - b[0].x)) < .5 && Math.abs((a[2].y - a[0].y) - (b[2].y - b[0].y)) < .5;
ok(sameShape(o.plates[0].poly, o.plates[1].poly) && X.LAYERS.every((L) => { const bs = o.bands.filter((b) => b.layer === L.key); return bs.length === 2 && sameShape(bs[0].poly, bs[1].poly); }), 'the plates stay identical: every band as deep as its fullest station, the same on every plate');
// (2) the items scale with the scene: the counter-scale follows the fit alone, capped as the board's embed is
ok(o.inv === +(o.fit.s * Math.min(1.45, 1 / Math.min(1, o.fit.s))).toFixed(3) && o.inv < 1 && o.inv > o.fit.s, 'the items\' scale is the board\'s rule at the fit — grown against a small scene, never past 1.45× of it (' + o.fit.s + ' → ' + o.inv + ')');
const big = X.layout({ turns: [{ mid: 'a', who: 'you', t: '', text: 'hi', read: [], say: [], made: [], land: [] }], sel: 'a' }, 'iso', 6000, 5000, {});
ok(big.fit.s >= 1 && big.inv === big.fit.s, 'a scene that fits at 1:1 or larger carries its items at the scene\'s own scale (' + big.fit.s + ')');
const tiny = X.layout(scene, 'iso', 400, 300, {}); ok(tiny.fit.s === 0.3 && tiny.inv === +(0.3 * 1.45).toFixed(3), 'the cap: 1.45× the scene, the board\'s embedded rule (' + tiny.inv + ')');
ok(!/--inv/.test(SRC.slice(SRC.indexOf('_applyPan() {'), SRC.indexOf('_click(e) {'))) && /view\.style\.setProperty\('--inv'/.test(SRC), 'the pan zoom never writes the counter-scale; the render sets it once from the fit');
ok(/vera-exploded \.xit\{[^}]*transform:translateY\(-100%\) scale\(var\(--inv,1\)\)/.test(SRC) && /vera-exploded \.xig\{[^}]*scale\(var\(--inv,1\)\)/.test(SRC) && /vera-exploded \.xnd\{[^}]*scale\(var\(--inv,1\)\)/.test(SRC), 'cards, widget groups and nodes counter-scale by the one --inv; the view\'s zoom transform carries them');
ok(!/xp-if-hd/.test(SRC) && !/xp-if\b/.test(SRC) && !/frameHtml/.test(SRC), 'no terminal-style frame with a three-dot header is left');
// (3) what was there stays: bands, +N more, Stack, Size, Blocks off, tip-in / flatten, images per tier, widgetOf, the events
const many = X.layout({ turns: [Object.assign({}, t, { made: t.made.concat(t.made, t.made) })], sel: 'm1' }, 'iso', 1200, 800, {});
ok(many.labels.some((l) => /more/.test(l.cls) && l.n === '+6 more') && many.widgets.filter((w) => w.layer === 'made').length === 6, 'six per band, the rest a count');
ok(o.bands.length === 10 && o.bands.filter((b) => b.empty).length === 4 && o.labels.some((l) => /empty/.test(l.cls) && l.n === 'nothing produced'), 'every band drawn, the empty ones say so');
for (const s of ['stack(on) {', 'widgetSize(s) {', 'tipIn() {', 'flatten(done) {', ':root[data-blocks="off"] vera-exploded .xit', 'vera-exploded.opening .xp-view{animation:xp-tip', 'vera-exploded.closing .xp-view{animation:xp-flat',
  'vera-exploded[data-den="hover"] .xit:hover .xp-img', 'vera-exploded[data-den="zen"] .xit.open .xp-img', "new CustomEvent('vera:xpl:pick'", "new CustomEvent('vera:xpl:place'", "new CustomEvent('vera:xpl:rendered'", "root.VeraISO.proj(30, 45, 1, true)", 'class="tpl"', "view: 'iso', full: false"]) ok(SRC.indexOf(s) >= 0, 'kept: ' + s);
ok(X.widgetOf({ kind: 'image', src: 'x.png' }).form === 'image' && X.widgetOf({ steps: [{ label: 'a' }] }).form === 'stepper' && X.widgetOf({ rows: [{ k: 'a', v: 1 }] }).form === 'kv', 'widgetOf maps kinds to forms');
ok(X.layout(scene, 'cards', 1200, 800, {}).cards.length === 12 && X.layout(scene, 'front', 1200, 800, {}).panels.length === 5, 'cards and front unchanged');
console.log(f ? f + ' failed' : 'all pass'); process.exit(f ? 1 : 0);
