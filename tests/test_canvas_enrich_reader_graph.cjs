// 2026-09-28 (owner):
//   * "the canvas items enrich:fabric.query & enrich:web.search and the other similar ones often dont have good results
//      and need to be collapsible"
//   * "the web one needs to be able to 'reader mode', text properly formatted and prettyfied and parsed and rendered like
//      markup from current bare scraped webpage and show the key body of text or a composite if its fragmented"
//   * "research caps need better outputs to the canvas"
//   * "the fabric one just seems to return one line results - itd be better if it displayed a vera graph .js graph by
//      default but the list can be an option"
//   node tests/test_canvas_enrich_reader_graph.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const CV = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const EMB = fs.readFileSync(path.join(__dirname, '..', 'vera', 'graph_embed_element.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'web', 'browser_capabilities.py'), 'utf8');

// ── reader markdown: the article's shape, drawn; nothing in it becomes markup ────────────────────────────────────
{
  const h = mod.mdx([
    '## Method', '', 'A paragraph with [a link](https://example.org/x) and **bold** and `code`.', '',
    '1. first', '2. second', '  - nested', '', '> a quote', '', '| Engine | Recall |', '| --- | --- |', '| A | 0.91 |', '',
    '![a chart](https://example.org/c.png)', '', '---', '', '```python', 'print(1)', '```', '',
    'escaped \\*not bold\\* and <script>alert(1)</script> and [x](javascript:alert(1))'].join('\n'));
  t('headings', /<h2>Method<\/h2>/.test(h));
  t('links open elsewhere', /<a href="https:\/\/example.org\/x" target="_blank" rel="noopener">a link<\/a>/.test(h));
  t('bold and inline code', /<strong>bold<\/strong>/.test(h) && /<code>code<\/code>/.test(h));
  t('numbered lists, with a nested list', /<ol><li>first<\/li><li>second<ul><li>nested<\/li><\/ul><\/li><\/ol>/.test(h), h.slice(h.indexOf('<ol>'), h.indexOf('</ol>') + 5));
  t('quotes', /<blockquote>a quote<\/blockquote>/.test(h));
  t('tables as tables', /<th>Engine<\/th><th>Recall<\/th>/.test(h) && /<td>A<\/td><td>0\.91<\/td>/.test(h));
  t('figures', /<figure><img class="vc-rd-img" src="https:\/\/example.org\/c.png" alt="a chart"/.test(h));
  t('rules and fenced code', /<hr>/.test(h) && /<pre class="vc-pre"><code>print\(1\)<\/code><\/pre>/.test(h));
  t('an escaped star stays a star', /escaped \*not bold\*/.test(h) && !/<em>not bold<\/em>/.test(h));
  t('a page\'s markup is text, never markup', !/<script>/.test(h) && /&lt;script&gt;/.test(h));
  t('only http(s) links become links', !/href="javascript:/.test(h));
}
{
  const h = mod.readerHtml({ title: 'Vector stores compared', site: 'Example', byline: 'A. Writer', published: '2026-09-01T10:00:00Z', minutes: 4, composite: true, parts: 3, markdown: 'Body text.' });
  t('the reader: its title, who, when, how long', /<h1 class="vc-rd-t">Vector stores compared<\/h1>/.test(h) && /Example · A\. Writer · 2026-09-01 · 4 min read/.test(h));
  t('and says when the body was stitched from a fragmented page', /stitched from 3 parts of a fragmented page/.test(h));
  t('and the article', /<div class="vc-rd-body"><p>Body text\.<\/p><\/div>/.test(h));
}

// ── the fabric's records as a graph ───────────────────────────────────────────────────────────────────────────────
{
  const g = mod.recGraph({ query: 'vector databases', items: [
    { id: 'a', title: 'Memgraph vector search', score: 0.69, meta: { dataset_id: 'topic.memgraph' }, tags: ['topic', 'web', 'discovery'] },
    { id: 'b', title: 'Neo4j vs Memgraph', score: 0.66, meta: { dataset_id: 'topic.memgraph' }, tags: ['topic', 'web', 'discovery'] },
    { id: 'c', title: 'W5-04 retrieval comparison', score: 0.65, meta: { dataset_id: 'board_index' }, tags: ['board', 'index'] },
    { id: 'd', title: 'Vector DB for the lab?', score: 0.65, meta: { dataset_id: 'agent_rag.gatherer' }, tags: ['topic'] } ] });
  const ids = g.nodes.map((n) => n.id);
  t('the query at the centre', ids[0] === 'q' && g.nodes[0].type === 'Query');
  t('the datasets that hold the records', ['d:topic.memgraph', 'd:board_index', 'd:agent_rag.gatherer'].every((x) => ids.includes(x)));
  t('each record under its dataset', g.edges.some((e) => e.from === 'd:topic.memgraph' && e.to === 'r:a') && g.edges.some((e) => e.from === 'q' && e.to === 'd:board_index'));
  t('a record is sized by how close it is', g.nodes.find((n) => n.id === 'r:a').r > 6);
  t('a tag two or more records share links them', ids.includes('t:web') && g.edges.some((e) => e.from === 'r:b' && e.to === 't:web'));
  t('a tag on one record alone is not drawn', !ids.includes('t:board'));
  t('and it says how much it holds', g.caption === '4 records');
}
{
  const el = {};
  const C = { title: 'From the fabric · vector databases', kind: 'memory', view: 'graph', query: 'vector databases', items: [{ id: 'a', title: 'A', score: 0.6 }] };
  const h = mod.BLOCK.records(C, 'l', 'enrich:fabric.query:x', el);
  t('the fabric item opens as a graph', /class="vc-live" data-live="rgraph" data-key="enrich:fabric.query:x"/.test(h));
  t('and the list is one click away', /data-rec-act="view"[^>]*data-rec-arg="graph"/.test(h) && /data-rec-act="view"[^>]*data-rec-arg="list"/.test(h));
  el._recUi['enrich:fabric.query:x'].view = 'list';
  t('the reader\'s choice wins after that', !/data-live="rgraph"/.test(mod.BLOCK.records(C, 'l', 'enrich:fabric.query:x', el)));
  const W = mod.BLOCK.records({ title: 'Web', kind: 'web', items: [{ id: 'u', url: 'https://x.org', title: 'X' }] }, 'l', 'k', {});
  t('a web list has no graph view', !/data-rec-arg="graph"/.test(W));
}
t('the live layer draws the graph through <vera-graph-embed>, fed the records', /else if \(kind === 'rgraph'\) \{ inner = document\.createElement\('vera-graph-embed'\)/.test(CV) && /inner\.setAttribute\('renderer', 'data'\)/.test(CV) && /this\._rgFeed\(inner, key\);/.test(CV));
t('a record\'s node opens it in the list', /_rgNode\(key, node\) \{/.test(CV) && /st\.view = 'list';/.test(CV));
/* measured on the mirror, 2026-09-29: vera_graph.js writes its stylesheet into <head>, which the canvas's shadow root does
   not see - the graph came up squashed with its workbench drawers as bare text */
t('the graph\'s stylesheet is carried into the column', /_adoptGraphCss\(tries\) \{/.test(CV) && /\/\\\.vg-\[a-z\]\/\.test\(s\.textContent/.test(CV) && /this\._adoptGraphCss\(\); \};/.test(CV));
t('and its workbench drawer is not drawn in a records graph', /\.lv\[data-kind="rgraph"\] \.vg-bottom-area\{display:none!important\}/.test(CV));
t('unfolding opens the item in place, so its graph is not cut off', /if \(on\) this\._open\.delete\(key\); else this\._open\.add\(key\);/.test(CV));
t('the embed takes a graph from its host - an addition, the snapshot path unchanged', /setGraph\(data\) \{/.test(EMB) && /this\._graph\.load\(this\._data\)/.test(EMB) && /await this\._graph\.fetchSnapshot\(layer, params\);/.test(EMB));

// ── reader mode ───────────────────────────────────────────────────────────────────────────────────────────────────
t('browser.reader exists and runs the extraction in the page', /"browser\.reader",/.test(BR) && /page\.evaluate\(_reader_js\(\), int\(max_chars or 60_000\)\)/.test(BR));
t('a record reads its page in reader mode', /const rd = await this\._readPage\(String\(it\.url\)\);/.test(CV) && /b\.reader \? `<div class="vc-rbx-body rd">\$\{readerHtml\(b\.reader\)\}<\/div>`/.test(CV));
t('so does a source item', /open && rdr \? `<div class="vc-src-body rd">\$\{readerHtml\(rdr\)\}<\/div>`/.test(CV));

// ── the fold ──────────────────────────────────────────────────────────────────────────────────────────────────────
t('every keyed item has a fold on its header', /<span class="fd" data-act="fold"/.test(CV) && /if \(act === 'fold'\) return this\._setFold\(key, !\(it && it\.classList\.contains\('ufold'\)\)\);/.test(CV));
t('a folded item is its header line', /const compact = \(wouldFold && !hovered\) \|\| ufold;/.test(CV));
t('what the canvas brings forth unasked starts folded', /return from === 'enrich' \|\| key\.indexOf\('enrich:'\) === 0;/.test(CV));
t('your fold is remembered per canvas', /localStorage\.setItem\('vera:canvas:fold:' \+ this\._foldsFor/.test(CV));
t('a click on a folded header opens it', /if \(it\.classList\.contains\('ufold'\)\) this\._setFold\(it\.dataset\.key, false\);/.test(CV));

// ── research on the canvas ────────────────────────────────────────────────────────────────────────────────────────
t('a document item draws with the reader\'s renderer', /markdown: c => `<div class="vc-md">\$\{mdx\(c\.md \|\| c\.text \|\| ''\)\}<\/div>`/.test(CV));
t('the report lands, with its citations', /_cvLandReport\(fullText, _cvRelMid\|\|'', jobId, cits\)/.test(CHAT) && /md\+='\\n\\n## Sources\\n\\n'/.test(CHAT));
t('the citations are kept from either path', /if\(m\.type==='citations'&&Array\.isArray\(m\.citations\)\) cits=m\.citations;/.test(CHAT) && /if\(Array\.isArray\(res\.citations\)\) cits=res\.citations;/.test(CHAT));

if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all ok');
