// 2026-09-27: Cap Ontology's matrix threw with every cap shown (caps x caps cells in one string); an item with no name field
// is named by what identifies it
//   node tests/test_capont_matrix_item_names.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const CO = R('vera/ontologies/cap_ontology_panel.html'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the matrix shows the most related caps and says how many more', /const MATRIX_MAX = 240;/.test(CO) && /caps = allCaps\.filter\(c => keep\.has\(c\)\); _capped = allCaps\.length - caps\.length;/.test(CO) && /most related of \$\{allCaps\.length\}/.test(CO));
t('an item is named by what identifies it', /String\(nameOf\(it\) \|\| it\.date \|\| it\.title \|\| idName\(it\) \|\| fb \|\| 'item'\)/.test(WE) && /const ID_KEYS = \['model'/.test(WE));
{
  const src = WE.slice(WE.indexOf("  const ID_KEYS = "), WE.indexOf("  const idName = ")) + WE.slice(WE.indexOf("  const idName = "), WE.indexOf("\n", WE.indexOf("  const idName = ") + 200) + 1);
  const body = WE.slice(WE.indexOf("  const ID_KEYS = "), WE.indexOf('const fieldVal'));
  const idName = new Function(body + '; return idName;')();
  t('a request log row is named by its model; a row with only an x_id by that', idName({ req_id: 'r1', model: 'qwen3:9b', ts: 1 }) === 'qwen3:9b' && idName({ run_x_id: 'z9', n: 1 }) === 'z9' && idName({ n: 1 }) === '');
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
