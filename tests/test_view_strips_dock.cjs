// 2026-09-27 (owner): "the research ui lhm needs integrating into the unified lhm and im sure there are more that are missing"
// - a sweep of every panel for a strip of view tabs found these six
//   node tests/test_view_strips_dock.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const PJ = R('vera/vera-panel.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a tab drawn as a div is an item; data-label names an item', /b\.matches\('\[role="tab"\], \.tab'\)/.test(PJ) && /var dl = \(b\.getAttribute\('data-label'\) \|\| ''\)\.trim\(\); if \(dl\) return dl;/.test(PJ));
const PAGES = { 'vera/vector browser/vector_browser_panel.html': '<div class="tab-bar" data-vera-lhm>', 'vera/homeassistant/ha_panel.html': '<div class="tabs" data-vera-lhm>', 'vera/machine learning/ml_lab_panel.html': '<div class="seg" role="tablist" data-vera-lhm>', 'vera/monitor/perf_panel.html': '<div class="tabs" data-vera-lhm>', 'vera/widgets/widget_registry_panel.html': '<span class="seg" id="view" data-vera-lhm>' };
for (const [p, mark] of Object.entries(PAGES)) { const s = R(p); t(p + ': its view strip docks, and the page loads the bridge', s.includes(mark) && /vera-panel-bridge\.js/.test(s)); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
