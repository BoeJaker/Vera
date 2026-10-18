// One asset search for the desk chart, the Markets page and the Quant Studio (owner 18 Oct: search other assets and navigate
// deeper; the redesign: elements derived from the panels, one implementation). <vera-mkt-lookup> lives in markets/chart.js.
'use strict';
const fs = require('fs'); const path = require('path'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const C = R('vera/markets/markets_chart.js'), P = R('vera/markets/markets_panel.html'), S = R('vera/markets/markets_studio_panel.html');
t('the element is defined once, in the shared chart library', C.includes("root.customElements.define('vera-mkt-lookup', VeraMktLookup)") && !P.includes("define('vera-mkt-lookup'") && !S.includes("define('vera-mkt-lookup'"));
t('it keeps the panel behaviour: 250 ms debounce, 8 results, arrows / Enter / Escape, a click outside closes it',
  C.includes("'/markets/lookup?query=' + encodeURIComponent(q) + '&limit=' + lim") && C.includes('}, 250); }') && C.includes("e.key === 'ArrowDown'") && C.includes("e.key === 'Escape'") && C.includes("document.addEventListener('click', this._out"));
t('a host may put rows above the assets (extra) and hears them as mkt-extra; assets as mkt-pick', C.includes("typeof this.extra === 'function'") && C.includes("new CustomEvent('mkt-extra'") && C.includes("new CustomEvent('mkt-pick'"));
t('the desk chart has the search and Back in its header', C.includes('<vera-mkt-lookup class="lk" placeholder="Search assets…"></vera-mkt-lookup>') && C.includes('data-a="back"') && C.includes("if (a === 'back')"));
t('a pick switches the chart and keeps history; the host hears mkt-symbol', C.includes('this._go(d.provider, d.symbol, true)') && C.includes("new CustomEvent('mkt-symbol'"));
t('the Markets page header uses the element; its own lookup code is gone', P.includes('<vera-mkt-lookup id="lkEl"') && P.includes("addEventListener('mkt-pick', (e) => { const d = e.detail || {}; if (d.key) loadKey(d.key, null, d.item); })") && !/function renderLk|function pickLk|lkInput|id="lkList"/.test(P));
t('the Quant Studio uses the element with its metrics as extra rows; its own search code is gone',
  S.includes('<script src="/markets/chart.js"></script>') && S.includes('<vera-mkt-lookup id="gsEl"') && S.includes("$('gsEl').extra = (q) => q.length >= 3 ? metricMatches(q)") &&
  S.includes("addEventListener('mkt-extra', (e) => { if (e.detail && e.detail.value) pickMetric(e.detail.value); })") && S.includes('async function pickSearchItem(it){') && !/gsIn|gsDd/.test(S));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
