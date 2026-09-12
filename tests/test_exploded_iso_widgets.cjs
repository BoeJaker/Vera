// The exploded scene's iso as the board draws it: every band drawn, every item an iso widget (its form), the galaxy
// on the plate, a placed widget drawn as the widget, Stack floors, size steps; widgetOf maps kinds to forms.
const path = require('path'); const X = require(path.join(__dirname, '..', 'vera', 'chat', 'exploded_element.js'));
const t = { mid: 'm1', who: 'you', t: '14:31', text: 'why is boot slow?', reply: 'Four starts, four full pulls.',
  read: [{ id: 'v1', n: 'fabric_capabilities.py', d: 'vector · 0.94', col: '#a78bfa', kind: 'chunk', score: .94, body: 'corpus = pull_all()' }, { id: 'm1', n: 'boot memo', d: 'memory · 0.71', col: '#5ec9a0', kind: 'memory', score: .71 }],
  rel: [{ from: 'm1', to: 'v1', kind: 'mem' }], say: [],
  made: [{ n: 'obs.provenance', d: 'capability · done', col: '#8fb87a', kind: 'cap', body: 'branch bleeding-edge\nreembeds 4' }, { n: 'Boot timings', d: 'widget · bars', col: '#a78bfa', kind: 'widget', form: 'bars', data: { a: 18, b: 17, c: 19 } }],
  land: [{ n: 'GPU', d: 'canvas · placed from the registry', col: '#8fb87a', kind: 'widget', tpl: 'thermo', form: 'thermo' }] };
const o = X.layout({ turns: [t, { mid: 'm2', who: 'you', t: '14:38', text: 'where?', read: [], say: [], made: [], land: [] }], sel: 'm1' }, 'iso', 1200, 800, {});
let f = 0; const ok = (c, m) => { console.log((c ? 'ok   ' : 'FAIL ') + m); if (!c) f++; };
ok(o.plates.length === 2 && o.plates[0].poly.length === 4, 'two plates');
ok(o.bands.length === 10, 'every band of every station is drawn (' + o.bands.length + ')');
ok(o.bands.filter((b) => b.empty).length === 4, 'the empty bands are marked (turn 2: graph · read · made · land; its exchange holds the question)');
ok(o.widgets.length === 8, 'every item an iso widget: 2 read · 2 exchange · 2 made · 1 landed · turn 2 question (' + o.widgets.length + ')');
const w = o.widgets.find((x) => x.layer === 'land'); ok(w && w.form === 'thermo' && w.placed && w.sample, 'a placed widget is drawn as its form (sample data until it reads)');
const b = o.widgets.find((x) => x.card.n === 'Boot timings'); ok(b && b.form === 'bars' && b.data.a === 18, 'a reply widget carries its form and data');
const c = o.widgets.find((x) => x.layer === 'made' && x.card.kind === 'cap'); ok(c && c.form === 'log', 'a capability card draws as a log');
const r = o.widgets.find((x) => x.layer === 'read' && x.card.id === 'm1'); ok(r && r.form === 'bar', 'a scored record draws as a meter');
ok(o.graphs.length === 1 && o.graphs[0].iso, 'the context galaxy lies on the plate');
ok(o.labels.some((l) => l.cls.includes('empty') && l.n === 'nothing produced'), 'an empty band says so');
ok(o.edges.some((e) => e.cls === 'in') && o.edges.some((e) => e.cls === 'out') && o.edges.some((e) => e.cls === 'link'), 'the runs between the pins');
const s = X.layout({ turns: [t, t], sel: 'm1' }, 'iso', 1200, 800, { stack: true, wsz: 'l' }); ok(s.stack && s.plates[0].z !== s.plates[1].z, 'Stack: the stations on floors'); ok(s.widgets[0].w === 108, 'size L frames');
ok(X.widgetOf({ kind: 'image', src: 'x.png' }).form === 'image' && X.widgetOf({ steps: [{ label: 'a' }] }).form === 'stepper' && X.widgetOf({ rows: [{ k: 'a', v: 1 }] }).form === 'kv', 'widgetOf maps kinds to forms');
ok(X.layout({ turns: [t], sel: 'm1' }, 'cards', 1200, 800, {}).cards.length === 7, 'cards mode unchanged');
console.log(f ? f + ' failed' : 'all pass'); process.exit(f ? 1 : 0);
