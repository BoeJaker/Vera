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
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
