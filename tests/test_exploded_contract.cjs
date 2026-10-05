// The code and prose explodes drawn as the exploded scene (cards · front · iso): contractScene() maps the contract
// (layers · groups · cards · edges) to stations, plates and runs; layout() draws a scene's own plates and links.
//   node tests/test_exploded_contract.cjs
const path = require('node:path');
const X = require(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const CODE = { ok: true, kind: 'code', layers: [{ id: 'code.ast', label: 'symbols · ast' }, { id: 'code.calls', label: 'calls' }, { id: 'code.contains', label: 'contains' }],
  groups: [{ id: 'f.py', label: 'f.py', kind: 'file', parent: null }, { id: 'f.py::Store', label: 'class Store', kind: 'class', parent: 'f.py' }, { id: 'ext', label: 'external', kind: 'external', parent: null }],
  cards: [{ id: 'f.py::<module>', group: 'f.py', layer: 'code.ast', kind: 'module', title: 'f.py', subtitle: 'module · 19 lines' },
    { id: 'f.py::Store', group: 'f.py', layer: 'code.ast', kind: 'class', title: 'Store', code: 'class Store:\n  pass' },
    { id: 'f.py::Store.load', group: 'f.py::Store', layer: 'code.ast', kind: 'method', title: 'load', fields: [{ k: 'returns', v: 'dict' }, { k: 'lines', v: '3' }] },
    { id: 'f.py::Store.save', group: 'f.py::Store', layer: 'code.ast', kind: 'method', title: 'save' },
    { id: 'f.py::main', group: 'f.py', layer: 'code.ast', kind: 'function', title: 'main' }, { id: 'os.replace', group: 'ext', layer: 'code.ast', kind: 'external', title: 'os.replace' }],
  edges: [{ from: 'f.py::Store', to: 'f.py::Store.load', layer: 'code.contains', kind: 'CONTAINS' }, { from: 'f.py::Store', to: 'f.py::Store.save', layer: 'code.contains', kind: 'CONTAINS' },
    { from: 'f.py::main', to: 'f.py::Store.load', layer: 'code.calls', kind: 'CALLS', label: 'calls' }, { from: 'f.py::Store.load', to: 'f.py::nowhere', kind: 'CALLS' }, { from: 'f.py::Store.save', to: 'os.replace', layer: 'code.calls', kind: 'CALLS' }] };
const s = X.contractScene(CODE, { label: 'python · 19 lines' });
t('a station per top-level group: the file holds its class\'s methods', s.turns.length === 1 && s.turns[0].who === 'file' && s.turns[0].text === 'f.py');
t('a plate per kind, in the reading order module · class · function · method', s.by === 'kind' && s.layers.map((L) => L.name).join(',') === 'module,class,function,method,external', s.layers.map((L) => L.name).join(','));
t('no plate key carries a colon (a card id in the scene is mid:layer:index)', s.layers.every((L) => L.key.indexOf(':') < 0) && s.turns.every((x) => x.mid.indexOf(':') < 0));
const tr = s.turns[0], mk = s.layers.find((L) => L.name === 'method').key;
t('the cards keep what they hold: code, rows, subtitle', tr[mk].length === 2 && tr[s.layers.find((L) => L.name === 'class').key][0].code === true && tr[mk][0].rows.length === 2 && tr[s.layers[0].key][0].d === 'module · 19 lines');
t('what the file uses but does not define stands in the station that calls it, not a station of its own', s.turns.length === 1 && tr[s.layers[4].key].length === 1 && s.across === 0);
t('the relations are the runs, an edge to nothing is dropped', tr.links.length === 4 && tr.links.some((l) => l.kind === 'CALLS' && l.title === 'calls'));
['cards', 'front', 'iso'].forEach((mode) => { const o = X.layout(s, mode, 1400, 900, {});
  const n = mode === 'front' ? o.panels.length : (mode === 'iso' ? o.bands.length : o.plates.length);
  const runs = mode === 'front' ? (o.frontRels || []).length : o.edges.filter((e) => /link/.test(e.cls || '')).length;
  t(mode + ': the structure\'s own plates, its cards, and its relations drawn as runs', n === 5 && runs >= 4 && (mode === 'front' || o.cards.length === 6), n + ' plates · ' + runs + ' runs · ' + o.cards.length + ' cards'); });
const PROSE = { ok: true, kind: 'prose', layers: [{ id: 'ner', label: 'entities' }, { id: 'rel.typed', label: 'relations' }],
  groups: [{ id: 'p1', label: '¶ 1', kind: 'paragraph', parent: null }, { id: 'p2', label: '¶ 2', kind: 'paragraph', parent: null }],
  cards: [{ id: 'a', group: 'p1', layer: 'ner', kind: 'person', title: 'Ada' }, { id: 'b', group: 'p1', layer: 'ner', kind: 'location', title: 'PBS-01' }, { id: 'c', group: 'p2', layer: 'ner', kind: 'date', title: 'August' }],
  edges: [{ from: 'a', to: 'b', layer: 'rel.typed', kind: 'RELATES', label: 'works at' }, { from: 'a', to: 'c', kind: 'RELATES' }] };
const p = X.contractScene(PROSE);
t('prose: a station per paragraph, a plate per entity kind; a relation across paragraphs is counted, not drawn', p.turns.length === 2 && p.layers.length === 3 && p.turns[0].links.length === 1 && p.across === 1);
t('the chat\'s own scene is untouched: five layers when a scene names none', X.layout({ turns: [{ mid: 'm1', who: 'you', text: 'hi', read: [], say: [], made: [], land: [] }] }, 'iso', 1200, 800, {}).bands.length === 5);
const SRC = require('node:fs').readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'), 'utf8');
t('iso navigates as Live ops does: zoom 5%-400%, ctrl-drag turns it, a double-click focuses a plate, the pan keeps 80 px in the frame',
  /const ZMIN = 0\.05, ZMAX = 4, KEEP = 80;/.test(SRC) && /rot: \(e\.ctrlKey \|\| e\.metaKey\) && this\._S\.mode === 'iso'/.test(SRC) && /this\._S\.isoFocus = hit\.layer && this\._S\.isoFocus !== hit\.layer \? hit\.layer : null;/.test(SRC)
  && /const mx = Math\.max\(0, W \* \(1 \+ p\.z\) \/ 2 - KEEP\)/.test(SRC) && !/Math\.max\(0\.4, Math\.min\(3,/.test(SRC));
// the iso routes as Live ops does: no run passes over a pin it does not end on; each run is one pipe
{ const big = JSON.parse(JSON.stringify(CODE)); for (let k = 0; k < 6; k++) big.cards.push({ id: 'f.py::fn' + k, group: 'f.py', layer: 'code.ast', kind: 'function', title: 'fn' + k });
  for (let k = 0; k < 6; k++) big.edges.push({ from: 'f.py::fn' + k, to: 'f.py::Store.' + (k % 2 ? 'load' : 'save'), kind: 'CALLS' }, { from: 'f.py::main', to: 'f.py::fn' + k, kind: 'CALLS' });
  const o = X.layout(X.contractScene(big), 'iso', 1400, 900, {}), pins = o.widgets.map((w) => ({ id: w.id, x: w.x, y: w.y }));
  const legs = o.edges.filter((e) => e.run && !e.tail), crossed = [];
  legs.forEach((e) => { const r = e.deg * Math.PI / 180, ux = Math.cos(r), uy = Math.sin(r); pins.forEach((p) => { const along = (p.x - e.x) * ux + (p.y - e.y) * uy, perp = Math.abs(-(p.x - e.x) * uy + (p.y - e.y) * ux); if (along > 2 && along < e.len - 2 && perp < 1.5) crossed.push([e.title, p.id]); }); });
  t('iso: channel routing - no run passes over a pin, every run is one svg pipe from pin to pin', legs.length > 20 && crossed.length === 0 && o.paths.filter((q) => !/trunk/.test(q.cls)).length === o.runs && o.paths.every((q) => /^M[\d.\- ]+(L[\d.\- ]+)+$/.test(q.d) && q.a && q.b), JSON.stringify({ legs: legs.length, crossed: crossed.slice(0, 4), paths: o.paths.length, runs: o.runs })); }
{ const hub = JSON.parse(JSON.stringify(CODE)); for (let k = 0; k < 5; k++) { hub.cards.push({ id: 'f.py::h' + k, group: 'f.py', layer: 'code.ast', kind: 'function', title: 'h' + k }); hub.edges.push({ from: 'f.py::h' + k, to: 'os.replace', kind: 'CALLS' }); }
  const o = X.layout(X.contractScene(hub), 'iso', 1400, 900, {}), tr = o.paths.filter((q) => /trunk/.test(q.cls));
  t('iso: three or more runs into one card join ONE trunk in its gutter - the trunk goes in once', new Set(tr.map((q) => q.run)).size === 1 && o.edges.filter((e) => /trunk/.test(e.cls)).length >= 2, JSON.stringify(o.paths.map((q) => q.cls))); }
t('the pipes are drawn as svg and light end to end under the pointer', /<svg class="xp-pipes"/.test(SRC) && /this\.classList\.toggle\('xp-routing', any\)/.test(SRC) && /vera-exploded\.xp-routing \.xp-pipe\.hot\{opacity:1/.test(SRC));
{ const big = JSON.parse(JSON.stringify(CODE)); for (let k = 0; k < 40; k++) big.cards.push({ id: 'f.py::g' + k, group: 'f.py', layer: 'code.ast', kind: 'function', title: 'g' + k });
  const o = X.layout(X.contractScene(big), 'iso', 1400, 900, {}), fn = o.widgets.filter((w) => w.card && /^g\d+$/.test(w.card.n));
  t('iso: a structure\'s plate holds forty functions, not twenty-four and "+N more"', fn.length === 40 && !o.labels.some((l) => /more/.test(l.cls) && /function/.test(l.k)), fn.length + ' drawn'); }
t('a new scene opens fitted: the last scene\'s zoom, pan, plate focus and open card do not carry over', /if \(sk !== this\._sceneKey\)/.test(SRC) && /this\._S\.isoFocus = null; this\._S\.focus = null;/.test(SRC));
{ const sc = X.contractScene(CODE), cards = [], links = []; sc.layers.forEach((L) => sc.turns.forEach((u) => (u[L.key] || []).forEach((c) => cards.push(c)))); sc.turns.forEach((u) => (u.links || []).forEach((l) => links.push(l)));
  const chat = { sel: 'm1', turns: [{ mid: 'm1', who: 'you', text: 'write a store', read: [], say: [], made: [{ n: 'python', d: 'code block', kind: 'code', body: 'class Store: ...' }], land: [], struct: cards, structLinks: links, structFrom: 'made:0' }] };
  const i = X.layout(chat, 'iso', 1400, 900, {}), c = X.layout(chat, 'cards', 1400, 900, {}), f = X.layout(chat, 'front', 1400, 900, {});
  t('a card exploded IN PLACE: the chat scene grows a structure plate in iso, cards and front - its cards, its relations, a run from the card',
    i.bands.some((b) => b.layer === 'struct') && i.widgets.filter((w) => w.layer === 'struct').length === cards.length && i.paths.some((q) => q.title === 'what this card holds') && i.paths.filter((q) => /link/.test(q.cls)).length >= links.length
    && c.plates.some((p) => p.layer === 'struct') && f.panels.some((p) => p.layer === 'struct') && i.bands.filter((b) => b.layer !== 'struct').length === 5, JSON.stringify({ bands: i.bands.map((b) => b.layer), w: i.widgets.length })); }
t('the double-click reads the whole code, and a card\'s mini mounts once its renderer is defined', /const b0 = typeof c\.body === 'string' \? c\.body : '', t0 = typeof c\.text === 'string' \? c\.text : '', body = t0\.length > b0\.length \? t0 : b0;   \/\* the whole code, not the 400/.test(SRC) && /whenDefined\('vera-structgraph'\)/.test(SRC));
console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);
