// The context graph's DETAIL (Notes/42 defect 48 — the old rail galaxy's features in the new graph): the record panel
// carries the record's text · URL · tags · type; the LIST rows; the search filters the list and dims the plot; the
// edge-type filter; the frames scrubber over the turns' snapshots; the mini's detail card and list for the widget form.
//   node tests/test_context_graph_detail.cjs
const path = require('node:path');
const F = require(path.join(__dirname, '..', 'vera', 'graph', 'families.js'));
global.VeraGraphFamilies = F;
const G = require(path.join(__dirname, '..', 'vera', 'chat', 'context_graph_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const nodes = [
  { id: 'v1', label: 'fabric_capabilities.py 410–486', source: 'vector', type: 'chunk', score: 0.94, text: 'def ensure(): pulls the model when the digest changed; the boot path compares the stored digest first', tags: ['fabric', 'boot'], included: true },
  { id: 'v2', label: 'vera_start.log', source: 'vector', type: 'chunk', score: 0.7, snippet: 'boot 4 · re-embed · 38 s', included: true },
  { id: 'g1', label: 'commit 312caef', source: 'graph', type: 'dataset', dataset: 'commits', score: 0.9, summary: 'gate the pull behind a stored digest', included: true },
  { id: 'w1', label: 'issue #41', source: 'web', type: 'page', score: 0.5, url: 'https://example.test/issues/41', content: 'fabric re-embeds on every restart', included: false },
  { id: 'c1', label: 'fabric.digest', source: 'cap', score: 0.6, included: true },
  { id: 'm1', label: 'recall 1', source: 'memory', score: 0.8, text: 'the digest gate closed #41', included: true },
];
const edges = [{ from: 'v1', to: 'g1', label: 'CITES' }, { from: 'v1', to: 'v2', type: 'SIMILAR' }, { from: 'g1', to: 'w1', label: 'RELATED' }, { from: 'c1', to: 'v1' }];
const base = { view: 'galaxy', nodes, edges, focus: ['v1', 'g1', 'm1', 'c1'], reads: { m4: ['v1', 'g1', 'm1', 'c1'] }, layersOff: new Set(), related: true, pan: { x: 0, y: 0, z: 1 }, allEdges: true };
const W = 660, H = 640;

// ── the text preview: the node's text, else snippet · summary · content · preview ──
t('textOf reads text first', G.textOf(nodes[0]).indexOf('def ensure') === 0);
t('textOf falls back to snippet · summary · content', G.textOf(nodes[1]) === 'boot 4 · re-embed · 38 s' && G.textOf(nodes[2]).indexOf('gate the pull') === 0 && G.textOf(nodes[3]).indexOf('fabric re-embeds') === 0);
t('textOf: nothing when the record has no text', G.textOf(nodes[4]) === '' && G.textOf(null) === '');

// ── the record panel: text · url · tags · type beside relevance · tokens · read by ──
const p1 = G.compute(Object.assign({}, base, { sel: 'v1' }), W, H);
const keys = (r) => r.rows.map((x) => x.k).join(',');
t('the panel carries relevance · tokens · read by · loop steps · source · type · tags', keys(p1.rec) === 'relevance,tokens,read by,loop steps,source,type,tags', keys(p1.rec));
t('the panel carries the record\'s text', p1.rec.text.indexOf('def ensure()') === 0);
t('the type row: the type and the dataset', G.compute(Object.assign({}, base, { sel: 'g1' }), W, H).rec.rows.find((x) => x.k === 'type').v === 'dataset · dataset commits');
const pw = G.compute(Object.assign({}, base, { sel: 'w1' }), W, H);
t('the url row links out', pw.rec.rows.find((x) => x.k === 'url') && pw.rec.rows.find((x) => x.k === 'url').url === 'https://example.test/issues/41' && pw.rec.url === 'https://example.test/issues/41');
t('a memory record carries its text too', G.compute(Object.assign({}, base, { sel: 'm1' }), W, H).rec.text === 'the digest gate closed #41');
const card = G.recordCard(p1.rec, {});
t('the card draws the text block, the rows, the actions', /cg-rec-t/.test(card) && /def ensure\(\)/.test(card) && /data-a="toggle" data-id="v1"/.test(card) && /data-a="open"/.test(card) && /Focus turn/.test(card));
t('the url row is an anchor in the card', /<a class="v" href="https:\/\/example\.test\/issues\/41"/.test(G.recordCard(pw.rec, {})));
t('the compact card (the mini\'s) trims the rows and the text', (G.recordCard(p1.rec, { compact: true }).match(/cg-rec-r/g) || []).length === 3 && /cg-rec compact/.test(G.recordCard(p1.rec, { compact: true })));

// ── the list: the rows (source dot · label · relevance bar · tokens · included), the most relevant first ──
const l0 = G.compute(base, W, H);
t('a row per record, the memory recall included, most relevant first', l0.list.length === 6 && l0.list[0].id === 'v1' && l0.list[l0.list.length - 1].id === 'w1', l0.list.map((r) => r.id).join(','));
t('a row knows its source colour, relevance, tokens, included, text', l0.list[0].col && l0.list[0].score === 0.94 && l0.list[0].tok > 0 && l0.list[0].included === true && l0.list.find((r) => r.id === 'w1').included === false && l0.list[0].text.indexOf('def ensure') === 0);
const lh = G.listHtml(l0, {});
t('the list draws rows with the dot, the bar, the tokens and the toggle', (lh.match(/class="cg-row/g) || []).length === 6 && /class="bar"><i style="width:94%"/.test(lh) && /data-a="toggle" data-id="w1"[^>]*>＋</.test(lh) && /6 records/.test(lh));
t('an excluded record is a hollow row', /cg-row excl[^"]*" data-id="w1"/.test(lh));

// ── the search: the list filters, the plot dims what does not match, the hits count ──
const s1 = G.compute(Object.assign({}, base, { q: 'digest' }), W, H);
t('a search matches on text, summary and label', s1.list.map((r) => r.id).sort().join(',') === 'c1,g1,m1,v1', s1.list.map((r) => r.id).join(','));
t('the hit count is the matches among the records drawn', s1.hits === 4, String(s1.hits));
t('what does not match dims in the plot (miss), what matches does not', s1.cnodes.filter((n) => /\bmiss\b/.test(n.cls)).map((n) => n.id).sort().join(',') === 'v2,w1' && !/\bmiss\b/.test(s1.cnodes.find((n) => n.id === 'v1').cls));
t('the memory node dims with the search too', G.compute(Object.assign({}, base, { q: 'zzz' }), W, H).memNodes.every((n) => /\bmiss\b/.test(n.cls)));
t('a search on a tag matches', G.compute(Object.assign({}, base, { q: 'boot' }), W, H).list.map((r) => r.id).indexOf('v1') >= 0);
t('no search: no misses, no hits, the whole list', l0.hits === 0 && l0.cnodes.every((n) => !/\bmiss\b/.test(n.cls)) && /6 records/.test(G.listHtml(l0)));
t('the list head says how many match', /4 of 6 match “digest”/.test(G.listHtml(s1)));

// ── the edge types: the chips with counts; a folded type leaves the plot and the panel ──
t('the edge types, by count, an untyped relation as RELATED', l0.edgeTypes.map((e) => e.name + ':' + e.n).join(',') === 'CITES:1,SIMILAR:1,RELATED:2' || l0.edgeTypes.map((e) => e.name + ':' + e.n).sort().join(',') === 'CITES:1,RELATED:2,SIMILAR:1', l0.edgeTypes.map((e) => e.name + ':' + e.n).join(','));
const relTitles = (o) => o.cedges.filter((e) => /^rel/.test(e.cls)).map((e) => e.title);
const eOff = G.compute(Object.assign({}, base, { edgesOff: new Set(['CITES']) }), W, H);
t('a folded edge type leaves the plot', relTitles(l0).some((s) => /cites/.test(s)) && !relTitles(eOff).some((s) => /cites/.test(s)) && relTitles(eOff).some((s) => /similar/.test(s)));
t('the chip says it is off', eOff.edgeTypes.find((e) => e.name === 'CITES').on === false && eOff.edgeTypes.find((e) => e.name === 'SIMILAR').on === true);
t('the panel\'s relations follow the filter', G.compute(Object.assign({}, base, { sel: 'v1' }), W, H).rec.rels.length === 3 && G.compute(Object.assign({}, base, { sel: 'v1', edgesOff: new Set(['CITES']) }), W, H).rec.rels.length === 2);

// ── the frames: a turn's snapshot stands in for the live records while picked; the live set is untouched ──
const frames = [{ id: 1001, label: 'm1', ts: '10:00', nodes: nodes.slice(0, 2), edges: [{ from: 'v1', to: 'v2', type: 'SIMILAR' }] }, { id: 1002, label: 'm2', ts: '10:02', nodes: nodes.slice(0, 4), edges: edges.slice(0, 3) }];
const fLive = G.compute(Object.assign({}, base, { frames }), W, H);
t('frames listed, none in view: the live set draws', fLive.frames.length === 2 && fLive.frames.every((f) => !f.on) && fLive.frame === null && fLive.cnodes.length === 5);
const f1 = G.compute(Object.assign({}, base, { frames, frame: 1001 }), W, H);
t('a picked frame draws its own records and relations', f1.frame && f1.frame.label === 'm1' && f1.cnodes.length === 2 && f1.cedges.filter((e) => /^rel/.test(e.cls)).length === 1 && f1.frames.find((f) => f.id === 1001).on === true, f1.cnodes.length + ' nodes');
t('a frame id compares as a string too', G.compute(Object.assign({}, base, { frames, frame: '1002' }), W, H).cnodes.length === 4);
t('the list head names the frame', /frame m1/.test(G.listHtml(f1)));
t('the live records are untouched by the scrub', base.nodes.length === 6 && base.nodes === nodes);

// ── the mini: the detail card, the list, and miniHtml with both ──
const st = G.stateFrom({ nodes, rels: edges, q: 'digest', list: true, frames, frame: 1002, edgesOff: ['SIMILAR'] });
t('stateFrom carries q · list · frames · frame · edgesOff', st.q === 'digest' && st.list === true && st.frames.length === 2 && st.frame === 1002 && st.edgesOff.has('SIMILAR'));
const md = G.miniDetail(G.stateFrom({ nodes, rels: edges }), 'v1');
t('miniDetail: the compact card for one record, with its text and toggle', /cg-rec compact/.test(md) && /def ensure/.test(md) && /data-a="toggle" data-id="v1"/.test(md) && /data-id="v1"/.test(md));
t('miniDetail: nothing for an unknown id', G.miniDetail(G.stateFrom({ nodes, rels: edges }), 'nope') === '');
const ml = G.miniList(G.stateFrom({ nodes, rels: edges }), { q: 'digest' });
t('miniList: the compact rows, the search applied', /cg-list compact/.test(ml) && (ml.match(/class="cg-row/g) || []).length === 4);
const mh = G.miniHtml(G.stateFrom({ nodes, rels: edges }), 262, 196, { detail: 'g1', list: true });
t('miniHtml with detail + list: the box carries the list and the card', /cg-mini listing/.test(mh) && /cg-list compact/.test(mh) && /cg-rec compact" data-id="g1"/.test(mh));
t('miniHtml without opts: no card, no list', !/cg-rec/.test(G.miniHtml(G.stateFrom({ nodes, rels: edges }), 262, 196)) && !/cg-list/.test(G.miniHtml(G.stateFrom({ nodes, rels: edges }), 262, 196)));
t('the API exposes the detail functions', typeof G.miniDetail === 'function' && typeof G.miniList === 'function' && typeof G.recordCard === 'function' && typeof G.listHtml === 'function' && G.version === 4);
// the list carries the session's memory records too (hollow, no toggle — not prompt records), after the context's
const sess = [{ id: 'ss1', record_type: 'session', summary: 'the session', created_at: '2026-09-11T09:59:00Z', importance: 0.9 }, { id: 'sf1', record_type: 'fact', text: 'a digest fact', created_at: '2026-09-11T10:02:00Z', importance: 0.3 }];
const ls = G.compute(Object.assign({}, base, { memory: sess, memEdges: [] }), W, H);
t('the session records are rows after the context\'s, hollow, without a toggle', ls.list.length === 8 && ls.list.slice(-2).every((r) => r.sess && !r.included) && !/data-a="toggle" data-id="sf1"/.test(G.listHtml(ls)) && /data-a="toggle" data-id="v1"/.test(G.listHtml(ls)));
const lq = G.compute(Object.assign({}, base, { memory: sess, memEdges: [], q: 'digest' }), W, H);
t('the search counts what the list shows: hits = rows', lq.hits === lq.list.length && lq.list.some((r) => r.id === 'sf1'), lq.hits + ' vs ' + lq.list.length);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
