// The market chart, deeper (owner 18 Oct: "navigate a bit deeper"; the redesign plan section 4): the asset's news, position,
// alerts and backtests as tabs of the desk chart, drawn by the Markets pages' own row code moved once into markets/chart.js
// (VeraMktChart.rows). The pages' rows must read exactly as before: tests/fixtures/mkt_rows_parity.json holds what the pages'
// own code drew for sample rows before the move.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const C = R('vera/markets/markets_chart.js'), P = R('vera/markets/markets_panel.html'), S = R('vera/markets/markets_studio_panel.html');
const FX = JSON.parse(R('tests/fixtures/mkt_rows_parity.json'));
const ctx = { window: {}, HTMLElement: class {} }; vm.createContext(ctx); vm.runInContext(C, ctx); const M = ctx.window.VeraMktChart;
const cut = (src, a, b) => { const i = src.indexOf(a); const j = src.indexOf(b, i); return i < 0 || j < 0 ? '' : src.slice(i, j); };
// each page's own formatters, as the page passes them
const pf = new Function(cut(P, 'function esc(s)', '\n') + '\n' + cut(P, 'function fmtPx(v)', 'function fmtPct') + '\n' + cut(P, 'function fmtPct(v)', '\n') + '\nreturn {esc, fmtPx, fmtPct};')();
const sf = new Function(cut(S, 'const esc=s=>', '\n') + '\n' + cut(S, 'const fmtPx=v=>', 'const fmtPct') + '\n' + cut(S, 'const fmtPct=(v,s=true)', '\n') + '\n' + cut(S, 'const fmtN=v=>', '\n') + '\nreturn {esc, fmtPx, fmtPct, fmtN};')();
const D = FX.data;
t('the Markets page backtest list reads as before', M.rows.backtests(D.backtests, pf) === FX.panel.backtests);
t('the Markets page backtest trades read as before (and "no trades")', M.rows.trades(D.trades, pf) === FX.panel.trades && M.rows.trades([], pf) === FX.panel.tradesEmpty);
t('the Quant Studio alert feed reads as before', M.rows.alerts(D.alerts, sf) === FX.studio.alerts);
t('the Quant Studio holdings table reads as before (and the empty book, the flat sim account)', M.rows.holdings(D.holdings, sf, true) === FX.studio.holdings && M.rows.holdings([], sf, true) === FX.studio.holdingsEmptyReal && M.rows.holdings([], sf, false) === FX.studio.holdingsEmptySim);
t('the pulse stats compute as before', JSON.stringify(D.closes.map((c) => M.computeStats(c))) === JSON.stringify(FX.studio.stats));
// the pulse cells' fill and the sparkline: the Studio's own code, its page globals become inputs - nothing else differs
const body = (fn) => { const s = fn.toString(); return s.slice(s.indexOf('{') + 1, s.lastIndexOf('}')); };
const fillWas = FX.src.fill.replace('miniCloses(key,300)', 'd.closes(key)').replace('drawSpark(cv,', 'd.spark(cv,').replace('sentIndex()', 'd.sent()');
t('the pulse cells fill as before (same code)', body(M.fillHoldings).includes(fillWas));
const sparkWas = body(new Function('return ' + FX.src.spark.trim())()).replace('const dpr=devicePixelRatio||1', 'const dpr=root.devicePixelRatio||1').replace('COL.up:COL.dn', 'c.up:c.dn').replace(/hexA\(/g, 'c.hexA(');
t('the sparkline draws as before (same code)', body(M.drawSpark).trim() === sparkWas.trim());
t('the Quant Studio headlines read as before (its first 6)', M.rows.news(D.headlines.slice(0, 6), sf) === FX.studio.news);
t('the pages draw with the shared rows; their own copies are gone',
  P.includes('return VeraMktChart.rows.trades(trades,{esc,fmtPct});') && P.includes('el.innerHTML=VeraMktChart.rows.backtests(r.backtests,{esc,fmtPct});') &&
  S.includes('host.innerHTML=VeraMktChart.rows.holdings(holdings,{esc,fmtN,fmtPct},isReal);') && S.includes('VeraMktChart.fillHoldings(host,{closes:k=>miniCloses(k,300),sent:sentIndex,fmtPx,fmtPct,spark:drawSpark});') &&
  S.includes('function computeStats(c){ return VeraMktChart.computeStats(c); }') && S.includes('function drawSpark(cv,vals,up){ return VeraMktChart.drawSpark(cv,vals,up,{up:COL.up,dn:COL.dn,hexA}); }') &&
  S.includes("$('alFeed').innerHTML=VeraMktChart.rows.alerts(alerts,{esc,fmtPx})") &&
  S.includes('html+=VeraMktChart.rows.news(news.headlines.slice(0,6),{esc});') && !S.includes('const DIR_CHIP=') && !/<td class="\$\{t\.ret_pct>0/.test(P) && (S.match(/class="condRow" style="opacity:/g) || []).length === 0 &&
  !S.includes("'<td><canvas class=\"hspark\"") && !S.includes('const w52=c.slice(-252)'));
t('the chart has the views as tabs: Chart, News, Position, Alerts, Backtests', ['chart', 'news', 'pos', 'alerts', 'bt'].every((v) => C.includes("{ id: '" + v + "'")) && C.includes('data-a="view"') && C.includes("if (a === 'view') { this._view(v); return; }"));
t('each view reads its asset only, drawn by the shared rows', C.includes("this._call('markets.news.feed', { symbol_key: key, limit: 8 })") && C.includes('M.rows.holdings(mine, F, true)') && C.includes('M.fillHoldings(host,') && C.includes('M.rows.alerts(mine, F)') && C.includes('M.rows.backtests(mine, F)') && C.includes("String(b.dataset_id || '').startsWith(dsp)"));
t('news is searched when opened, at most every 10 minutes per asset (never on a timer)', C.includes('const NEWS_TTL = 600000;') && C.includes('Date.now() - r.at > NEWS_TTL') && !/setInterval\([^)]*_side/.test(C));
t('a backtest picked puts its trades on the candles, on its timeframe; another asset clears them',
  C.includes("this._call('markets.backtest.get', { id })") && C.includes('this._entries = tr.map((t) => t.entry_t)') && C.includes("this.setAttribute('tf', tf)") && C.includes("this._entries = []; this._exits = []; this._btNote = '';"));
t('another asset without the timeframe on show opens on one it has (the daily, else its first)', C.includes("if (Array.isArray(this._tfsOf) && !this._tfsOf.includes(this._tf)) { this.setAttribute('tf', this._tfsOf.includes('1d') ? '1d' : this._tfsOf[0]); return; }"));
t('a stale answer never draws over a newer view', C.includes('if (this._sideId !== want) return; side.innerHTML = html;'));
t('the views are navigation only: no order, arm or stop calls from the chart', !/broker|markets\.order|strategy\/archive|alerts\.ack|tx_add|tx_remove/.test(C));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
