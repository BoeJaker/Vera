// 2026-10-01: a tile as tall as what it holds, the default layout packed per section, and no holes beside a tile
//   node tests/test_dash_fit_and_pack.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const VD = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

// lift a function (or a var) out of the source by its own text, so the test runs the code the page runs
function lift(re) { const m = VD.match(re); if (!m) throw new Error('not found: ' + re); return m[0]; }
function fnText(name) {
  const i = VD.indexOf('function ' + name + '('); if (i < 0) throw new Error('no function ' + name);
  let d = 0, j = VD.indexOf('{', i);
  for (let k = j; k < VD.length; k++) { if (VD[k] === '{') d++; else if (VD[k] === '}') { d--; if (!d) return VD.slice(i, k + 1); } }
  throw new Error('unbalanced ' + name);
}
const shapeOf = lift(/var SHAPE_OF = \{[^;]*\};/);
const fitCat = lift(/var FIT_CAT = \{[^;]*\};/);
const api = new Function(shapeOf + '\n' + fitCat + '\n' + fnText('fitCategory') + '\n' + fnText('sparseFlow') + '\n' + fnText('flow') + '\n' + fnText('arrange') +
  '\nvar GRID = { cols: 12 };\nreturn { fitCategory: fitCategory, sparseFlow: sparseFlow, arrange: arrange };')();

// ── which tiles follow their content ──
t('a table, a log and a ranking follow their items', api.fitCategory('table') === 'items' && api.fitCategory('log') === 'events' && api.fitCategory('ranked') === 'rowsv');
t('a figure is two rows, chips by count', api.fitCategory('counter') === 'level' && api.fitCategory('pills') === 'chips');
t('graphs, maps, panels, composites and drawn visuals keep their author\'s height',
  ['vgraph', 'graph', 'map', 'panel', 'composite', 'section', 'cellmap', 'thermo', 'temps', 'gauge', 'column', 'pulse'].every((f) => api.fitCategory(f) === ''));

// ── the page's own placement is sparse: a section is a wall ──
const sp = api.sparseFlow([{ wid: 'a', span: [6, 4] }, { wid: 'b', span: [4, 4] }, { wid: 's', span: [12, 1] }, { wid: 'c', span: [2, 2] }], 12);
t('sparse placement never back-fills above a later full-width tile', sp[3].at[1] === 5 && sp[2].at[1] === 4);

// ── packing a section: tall first, then laid in reading order ──
const tiles = [{ wid: 'small', span: [3, 2] }, { wid: 'tall', span: [6, 7] }, { wid: 'mid', span: [4, 5] }, { wid: 'small2', span: [3, 2] }];
const sorted = tiles.slice().sort((a, b) => (b.span[1] - a.span[1]) || (b.span[0] - a.span[0]));
const order = api.arrange(sorted, 12).map((x) => x.wid);
t('the tall tiles lead a packed section', order[0] === 'tall' && order[1] === 'mid');
const placed = api.sparseFlow(order.map((w) => tiles.find((x) => x.wid === w)), 12);
t('the short tiles sit beside the tall ones, not below them', placed.filter((p) => p.wid.startsWith('small')).every((p) => p.at[1] < 7));

// ── the wiring ──
t('heights refit as each widget renders', VD.includes("grid.addEventListener('widget:rendered', fitSoon);"));
t('a height the user dragged is never refit', /userSized\(w\.dataset\.wid\)\) return;/.test(VD) && VD.includes('state.userH[w.dataset.wid] = 1'));
t('a layout the user saved is never reordered', /function packDefault\(\) \{\s*if \(state\.saved\) return;/.test(VD));
t('holes are filled only on the full-width grid, and a dragged width keeps its own', VD.includes('if (live !== cols) return;') && VD.includes('state.userH && state.userH[t.wid]) return;'));
t('changing a width drops the stretch first', (VD.match(/w\.removeAttribute\('data-vd-fill'\); w\.style\.gridColumn = ''; w\.classList\.add\('w-w'/g) || []).length === 3);
t('the grid keeps its own sparse placement (sections stay walls)', !/gridAutoFlow\s*=\s*'row dense'/.test(VD));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
