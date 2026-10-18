// WSU-75 (owner 2026-10-16: "full parity, shared code"): one markets chart for the Markets panel and the desk.
//   node tests/test_markets_chart_shared.cjs
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const root = path.join(__dirname, '..');
const J = fs.readFileSync(path.join(root, 'vera', 'markets', 'markets_chart.js'), 'utf8');
const P = fs.readFileSync(path.join(root, 'vera', 'markets', 'markets_panel.html'), 'utf8');
const C = fs.readFileSync(path.join(root, 'vera', 'canvas', 'canvas_element.js'), 'utf8');
const PY = fs.readFileSync(path.join(root, 'vera', 'markets', 'markets_capabilities.py'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

/* the module's pure helpers, run */
const win = { customElements: null };
vm.runInNewContext(J, { window: win, document: {}, HTMLElement: class {}, console });
const M = win.VeraMktChart;
t('the module exposes the shared pieces', M && ['ensureLw', 'lwCandle', 'lwHist', 'lwLine', 'seriesData', 'drawIndicators', 'setMarkers', 'syncGroup', 'dsId'].every((k) => typeof M[k] === 'function'));
t('dataset ids as the panel makes them', M.dsId('yahoo', 'SPY', '1d') === 'mkt.yahoo.spy.1d' && M.dsId('binance', 'BTC/USDT', '1h') === 'mkt.binance.btc_usdt.1h');
t('nulls stay as whitespace items (panes keep one index)', JSON.stringify(M.seriesData([1, 2], [null, 5])) === JSON.stringify([{ time: 1 }, { time: 2, value: 5 }]));
t('volume coloured by the candle', M.volumeData({ t: [1, 2], o: [1, 2], c: [2, 1], v: [10, 20] })[1].color === 'rgba(224,112,90,.4)');
/* drawIndicators over fakes: an overlay on the main chart, RSI and MACD in panes of their own */
const made = { main: [], subs: [] };
const fakeChart = (bag) => ({ addLineSeries: () => { const s = { setData() {}, createPriceLine() {} }; bag.push('line'); return s; }, addHistogramSeries: () => { bag.push('hist'); return { setData() {} }; }, timeScale: () => ({ getVisibleLogicalRange: () => null, setVisibleLogicalRange() {} }) });
const main = fakeChart(made.main); const overlays = [];
M.drawIndicators({ main, bars: { t: [1, 2, 3] }, overlays, esc: (s) => s,
  result: { t: [1, 2, 3], indicators: [{ id: 'e', kind: 'ema', pane: 'main', series: { ema: [1, 2, 3] } }, { id: 'r', kind: 'rsi', pane: 'sub', series: { rsi: [50, 60, 70] } }, { id: 'm', kind: 'macd', pane: 'sub', series: { macd: [1, 2, 3], macd_signal: [1, 1, 1], macd_hist: [0, 1, -1] } }] },
  addSub: (id) => { const bag = []; made.subs.push({ id, bag }); return { chart: fakeChart(bag) }; } });
t('an overlay is drawn on the price chart', overlays.length === 1 && made.main.length === 1);
t('RSI and MACD each get a pane of their own', made.subs.map((s) => s.id).join(',') === 'r,m' && made.subs[1].bag.join(',') === 'hist,line,line');

/* the panel uses them */
t('the panel loads the shared module before its own script', /<script src="\/markets\/chart\.js"><\/script>\s*<script>/.test(P));
t('the panel\'s chart helpers are the shared ones', P.includes('function ensureLw(){ return VeraMktChart.ensureLw(); }') && P.includes('const CHART_OPTS=VeraMktChart.CHART_OPTS;') && P.includes('function lwLine(c,o){ return VeraMktChart.lwLine(c,o); }'));
t('the panel draws its indicators and markers through the module', P.includes('VeraMktChart.drawIndicators({ main:CH.main, bars:B, result:r, addSub:addSubPane, overlays:CH.overlays, esc });') && P.includes('_sigMarkers=VeraMktChart.setMarkers(CH.candle,entries,exits,_sigMarkers);'));
t('the module is served at /markets/chart.js', PY.includes('@APP.get("/markets/chart.js", include_in_schema=False)'));

/* the desk uses them */
t('a candles record naming its symbol is the markets chart on the desk', C.includes("if (form === 'candles' && md && md.symbol) {") && C.includes('data-live="mchart"'));
t('the live layer mounts <vera-mkt-chart> from the shared module', C.includes("inner = document.createElement('vera-mkt-chart');") && C.includes("ensureLib('/markets/chart.js', 'vera-mkt-chart');"));
t('the desk chart: timeframes stored for the asset, indicators as toggles, the bar under the pointer, live while seen', J.includes("const w = await api('/markets/watchlist');") && J.includes("data-a=\"ind\"") && J.includes('subscribeCrosshairMove') && J.includes('this._io = new IntersectionObserver'));
t('a card shows the price; the panes of their own when it is larger', J.includes("this._small = (this.clientHeight || 0) < 420;"));
t('the charts size themselves to their panes and have a ground', J.includes('autoSize: true') && J.includes('background:var(--bg1,#15181d)'));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
