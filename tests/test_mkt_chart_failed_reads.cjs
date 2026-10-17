// WSU-86 / WSU-92 (owner 2026-10-17: the desk chart "cant zoom, pan, use the buttons"): a read that fails - the server
// restarting, a 5xx, an answer that is not a bars answer - never tears the chart down or is taken as "no bars".
//   node tests/test_mkt_chart_failed_reads.cjs
'use strict';
const fs = require('fs'); const path = require('path');
const S = fs.readFileSync(path.join(__dirname, '..', 'vera', 'markets', 'markets_chart.js'), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
t('a fetch that throws, or a 5xx, is a failed read', S.includes("if (r.status >= 500) return { error: 'HTTP ' + r.status, failed: true };") && S.includes("catch (e) { return { error: String(e), failed: true }; }"));
t('a bars answer with no count and no error is failed too (a stored dataset with no bars says count 0)', S.includes("if (!r || r.failed || (typeof r.count !== 'number' && !r.error)) {"));
t('a failed read keeps the drawing and retries with backoff (2, 4, 8 ... at most 60 s)', S.includes("'read failed - retrying'") && S.includes('1000 * Math.min(60, 2 ** this._retryN)') && S.includes("if (!this._B) this._msg('Reading '"));
t('the retry stops when the chart leaves the page', S.includes('disconnectedCallback() { clearTimeout(this._liveT); clearTimeout(this._retryT);'));
t('a failed watchlist or timeframe probe leaves the timeframes unknown (asked again), never "every timeframe"', S.includes('const wFailed = !w || !!w.failed || !Array.isArray(w.watchlist);') && S.includes('if (got.some((g) => g === undefined)) { this._tfsOf = undefined; }') && S.includes('this._tfsOf === undefined ? [...new Set([this._goodTf || this._tf, this._tf])]'));
t('an empty answer is read again (15, 30, 60 s) before No stored bars is believed', S.includes("const again = !(r && r.error) && (this._emptyN || 0) < 3;") && S.includes('15000 * 2 ** (this._emptyN - 1)') && S.includes("' - checking again'"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);