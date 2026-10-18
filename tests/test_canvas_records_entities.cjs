// canvas-enrichment slice 6 (second part): a records item, by what its pages name - the reader cache's entities for each
// row's page (mode=cache, never the web, held in the element only) become "names" facets beside the sites, a filter, and
// each row's own top three; the page's reading comes with it, so a row opens at once
//   node tests/test_canvas_records_entities.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const C = R('vera/canvas/canvas_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the browser state carries a chosen name', C.includes("view: 'list', dom: '', ent: '', open: {}, body: {} }); };"));
t('a records item asks the cache for its rows\' pages, twenty a time, never writing the canvas', C.includes("if (!b || b.type !== 'records' || b.key == null")
  && C.includes("this.callResult('reader.page', { url: String(r.url), max_chars: 40000, mode: 'cache' })") && C.includes('.slice(0, 20).forEach((r) => {')
  && C.includes("this._srcProbed.add(k + '|' + r.id)"));
t('...keeping the salient entities in the element and the reading in the row\'s state', C.includes('em[String(r.id)] = (Array.isArray(x.entities) ? x.entities : [])')
  && C.includes('st.body[r.id] = Object.assign({}, had, { reader });'));
t('names two or more pages share are facets', C.includes('const entList = Object.keys(entN).filter((n) => entN[n] >= 2)') && C.includes('data-rec-act="ent"'));
t('...choosing one filters the rows, choosing it again clears it', C.includes('(!st.ent || entsOf(r).some((e) => e[0] === st.ent))')
  && C.includes("if (act === 'ent') { st.ent = st.ent === arg ? '' : arg; st.page = 0; return redraw(); }"));
t('each row shows its own top three', C.includes('entsOf(r).slice(0, 3).map((e) =>') && C.includes('.vc-rbx-ents{'));
// the research run: its pages and the names they share as a graph; its sites as a bar; its dates as a line
{
  const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
  const c = { query: 'crypto', items: [{ id: 'a', title: 'Bitcoin', domain: 'w.org' }, { id: 'b', title: 'Ethereum', domain: 'w.org' }, { id: 'c', title: 'Gold', domain: 'x.com' }] };
  const ents = { a: [['Satoshi Nakamoto', 'PERSON'], ['BTC', 'TICKER']], b: [['BTC', 'TICKER'], ['Vitalik Buterin', 'PERSON']], c: [['Satoshi Nakamoto', 'PERSON']] };
  const g = mod.recGraph(c, {}, ents);
  const ids = g.nodes.map((n) => n.id);
  t('shared names are nodes, a name one page carries is not', ids.includes('e:Satoshi Nakamoto') && ids.includes('e:BTC') && !ids.includes('e:Vitalik Buterin'));
  t('...linked to each page that names it, typed for colour', g.edges.filter((e) => e.to === 'e:BTC' && e.rel === 'names').length === 2
    && g.nodes.find((n) => n.id === 'e:Satoshi Nakamoto').type === 'Person' && /2 names they share/.test(g.caption));
  t('...and without names the graph is as it was', !mod.recGraph(c, {}).nodes.some((n) => String(n.id).startsWith('e:')));
  t('a list with shared names offers the graph view; a name node opens the list at its pages', C.includes("const canGraph = c.kind === 'memory' || c.view === 'graph' || entList.length > 0;")
    && C.includes("if (id.indexOf('e:') === 0) { const st = recState(this, key); st.view = 'list'; st.ent = id.slice(2);"));
  t('a research or web answer shows its sites as one bar (a site chooses it) and its dates as a line (a dot opens its page)',
    C.includes("if ((c.kind === 'research' || c.kind === 'web' || c.kind === 'evidence') && all.length > 2)") && C.includes('<div class="vc-rbx-map"') && C.includes('data-rec-act="goto"')
    && C.includes("if (act === 'goto') return this._rgNode(key, { id: 'r:' + arg });"));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
