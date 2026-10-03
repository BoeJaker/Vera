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
console.log(fails ? 'FAILED ' + fails + ' check(s)' : 'ALL OK'); process.exit(fails ? 1 : 0);
