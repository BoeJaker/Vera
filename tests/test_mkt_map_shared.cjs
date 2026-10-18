// The market map, one implementation (the redesign plan section 4: the Quant Studio's views as elements): the Studio's Pulse
// treemap moved once into markets/chart.js (VeraMktChart.drawMarketMap); the Studio calls it, and <vera-mkt-map> puts it on the
// desk. tests/fixtures/mkt_map_parity.json holds the Studio's own drawTreemap from before the move.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const C = R('vera/markets/markets_chart.js'), S = R('vera/markets/markets_studio_panel.html');
const FX = JSON.parse(R('tests/fixtures/mkt_map_parity.json'));
const ctx = { window: {}, HTMLElement: class {} }; vm.createContext(ctx); vm.runInContext(C, ctx); const M = ctx.window.VeraMktChart;
const body = (src) => { const s = String(src); return s.slice(s.indexOf('{') + 1, s.lastIndexOf('}')).trim(); };
// the Studio's map with its page globals as inputs: nothing else differs
const was = body(FX.drawTreemap)
  .replace("const cv=$('treemapCv'); if(!cv||!S.pulse) return;\n  const box=$('treemapBox');\n", "if(!cv||!pulse) return;\n  const allAssets=()=>pulse.groups.flatMap(g=>g.assets.filter(a=>!a.sparse));\n  const COL=o.col, hexA=o.hexA, fmtPct=o.fmtPct;\n")
  .replace('const dpr=devicePixelRatio||1', 'const dpr=root.devicePixelRatio||1')
  .replace("\n  const Wk=S.pulseWin;", '').replace('const groups=S.pulse.groups', 'const groups=pulse.groups')
  .replace('dur=ANIM_ON?520:1', 'dur=o.anim?520:1').replace(/\bTM\./g, 'st.').replace('assetDrill(key)', 'o.onPick(key)');
t('the Studio\'s market map draws as before (the same code, its page globals as inputs)', body(M.drawMarketMap.toString()) === was);
t('the Studio calls the shared map; its own copy is gone', S.includes("VeraMktChart.drawMarketMap($('treemapCv'),$('treemapBox'),S.pulse,S.pulseWin,TM,{col:COL,hexA,fmtPct,anim:ANIM_ON,onPick:assetDrill});") && !S.includes('ctx.roundRect(r.x,r.y'));
t('the windows are the Studio\'s', JSON.stringify(M.MAP_WINDOWS.map((w) => w[0])) === JSON.stringify(['chg_1d', 'chg_1w', 'chg_1m', 'chg_3m', 'chg_ytd', 'chg_1y']) && ['chg_1d', 'chg_1w', 'chg_1m', 'chg_3m', 'chg_ytd', 'chg_1y'].every((w) => S.includes('data-w="' + w + '"')));
t('<vera-mkt-map> is defined once, in the shared library', C.includes("root.customElements.define('vera-mkt-map', VeraMktMap)") && !S.includes("define('vera-mkt-map'"));
t('it reads the overview and draws it with the shared map', C.includes("await api('/markets/overview')") && C.includes('M.drawMarketMap(box.querySelector(\'canvas\'), box, P, w, this._st,'));
t('read again every 60 s only while seen and the page visible', C.includes("this._liveT = setTimeout(() => this._load(), 60000)") && C.includes("if (!this._seen || document.visibilityState === 'hidden') return; this._liveT"));
t('an asset picked opens its chart in place; ← Map returns', C.includes("const c = document.createElement('vera-mkt-chart');") && C.includes('data-a="map"') && C.includes('_map() {'));
// the desk: a markets.overview answer is the map
const CV = R('vera/canvas/canvas_element.js'), MS = R('vera/markets/markets_studio_capabilities.py');
t('markets.overview declares the treemap form over its groups', MS.includes("contract={\"presentation\": {'kind': 'parts', 'form': 'treemap', 'map': {'parts': 'groups', 'name': 'name', 'value': 'count'},"));
t('the canvas mounts <vera-mkt-map> for a treemap holding the overview\'s groups of assets (as candles + symbol mount the chart)',
  CV.includes("if (form === 'treemap' && md && Array.isArray(md.groups) && md.groups.some((g) => g && Array.isArray(g.assets))) {") && CV.includes('data-live="mmap"') &&
  CV.includes("else if (kind === 'mmap') { inner = document.createElement('vera-mkt-map');") && CV.includes("ensureLib('/markets/chart.js', 'vera-mkt-map')"));
t('a plain treemap (no groups of assets) stays the treemap form', (() => { const i = CV.indexOf("if (form === 'treemap' && md"); return i > 0 && CV.indexOf("data-live=\"widget\"", i) > i; })());
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
