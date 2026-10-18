// The Quant Studio's Portfolio (the real book), one implementation (the redesign plan section 4: the Studio's views as elements):
// its holdings + tiles moved once into markets/chart.js (VeraMktChart.realBook / tilesHtml); the Studio and the desk chart's Position
// tab use them, and <vera-mkt-folio> puts the Portfolio on the desk. tests/fixtures/mkt_folio_parity.json holds what the Studio's
// own code made of sample answers before the move.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const C = R('vera/markets/markets_chart.js'), S = R('vera/markets/markets_studio_panel.html'), CV = R('vera/canvas/canvas_element.js');
const FX = JSON.parse(R('tests/fixtures/mkt_folio_parity.json'));
const ctx = { window: {}, HTMLElement: class {} }; vm.createContext(ctx); vm.runInContext(C, ctx); const M = ctx.window.VeraMktChart;
const cut = (a, b) => { const i = S.indexOf(a), j = S.indexOf(b, i); return i < 0 || j < 0 ? '' : S.slice(i, j); };
const sf = new Function(cut('const fmtN=v=>', '\n') + '\nreturn {fmtN};')();
t('the real book reads as before (holdings and tiles, four answers incl. empty and none)', FX.answers.every((a, i) => JSON.stringify(M.realBook(a, sf)) === JSON.stringify(FX.books[i])));
t('the tiles draw as before', FX.books.every((b, i) => M.tilesHtml(b.tiles) === FX.tiles[i]));
t('the Studio uses the shared pieces; its own copies are gone', S.includes('const rb=VeraMktChart.realBook(pos,{fmtN});') && S.includes("$('foTiles').innerHTML=VeraMktChart.tilesHtml(tiles);") && !cut('async function renderAccounts(){', 'function renderHoldingsTable(').includes("['concentration',"));   /* the older renderFolio (replaced at load) is left as it was */
t('the chart\'s Position tab reads the same holdings', C.includes('const mine = M.realBook(p, F).holdings.filter((x) => x.symbol_key === key);') && C.includes('M.fillHoldings(host, holdFill(this))'));
t('<vera-mkt-folio> is defined once and draws the tiles + the holdings table read-only', C.includes("root.customElements.define('vera-mkt-folio', VeraMktFolio)") && C.includes('M.tilesHtml(rb.tiles)') && C.includes('M.rows.holdings(rb.holdings, F, true)') && !/class VeraMktFolio[\s\S]*?(tx_add|tx_remove|portfolio\/tx)/.test(C.slice(C.indexOf('class VeraMktFolio'), C.indexOf("define('vera-mkt-folio'"))));
t('a holding opens its chart in place; read again every 60 s while seen', C.includes('_back() {') && C.includes("this._liveT = setTimeout(() => this._load(), 60000); }"));
t('markets.portfolio.positions declares spark-table; the canvas mounts the folio for positions + totals', R('vera/markets/markets_lab_capabilities.py').includes('"form": "spark-table", "map": {"rows": "positions"') &&
  CV.includes("if (form === 'spark-table' && md && Array.isArray(md.positions) && md.totals && typeof md.totals === 'object') {") && CV.includes("inner = document.createElement('vera-mkt-folio')"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
