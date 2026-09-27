// 2026-09-27 (owner): "the lhm top left burger option in the harness lhm it is very hard to click ... the icons at the bottom
// need to hide ... some text on the top bar is partially visible when the lhm is collapsed ... when you click the burger bar
// and go back there are 2 headers" · "the chat lhm still loads in the legacy view first" · "the theme doesnt change over"
//   node tests/test_lhm_header_load_theme.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const UI = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-ui.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('the edge strip no longer lies over the menu (and its ☰) in LHM mode', /\.body-wrap\.lhm \.tab-hover-zone,\.body-wrap\.lhm \.tab-hover-zone\.active\{display:none!important\}/.test(HAR));
t('folded is icons only: the foot, the header words and a docked list go', /#lhmNav\.folded \.lhm-nav-foot,#lhmNav\.folded #lhmEventsFeed\{display:none!important\}/.test(HAR) && /#lhmNav\.folded \.lhm-topbar \.lhm-tt\{display:none\}/.test(HAR) && /#lhmNav\.folded \.lhm-absorbed \.lhm-tabs\{display:none\}/.test(HAR));
t('folded by « or as auto-hide\'s rail', /nav\.classList\.toggle\('folded', hidden \|\| nav\.classList\.contains\('railed'\)\);/.test(HAR));
t('one header: the absorb path draws no bar once swapped', /function _lhmRenderAbsorbed\(host, pid, nav\)\{\n  if\(_lhmAbsorbTop\) return false;/.test(HAR) && !/the caller draws the ordinary tab list under the bar/.test(HAR));
t('the list\'s own ☰ goes back to the panel\'s menu', /top: _lhmTopBack\(\) \|\| \{ title: 'Vera'/.test(HAR) && /toggle: \(\) => \{ _lhmAbsorbTop = false; _lhmNavSync\(\); \}/.test(HAR));
t('the chat is revealed only after it is parsed (a slow download cannot show the legacy markup)', /document\.addEventListener\('DOMContentLoaded', function\(\)\{ setTimeout\(function\(\)\{ de\.classList\.remove\('vboot'\); \}, 5000\); \}\);/.test(CHAT) && !/remove\('vboot'\); \}, 9000\)/.test(CHAT));

// the theme: switched by name alone, the new theme's values are read - not the old inline ones
{
  const a = UI.indexOf('  var _inlineThemeKeys = {};'), b = UI.indexOf('  function setThemeLocal(id, vars){'), c = UI.indexOf('  // ── 2a. One baseline under every panel');
  t('the theme code is where it was', a > 0 && b > a && c > b);
  const inline = {}, SHEET = { dusk: { '--bg': '#0e0f12', '--t1': '#d4dae4', '--ac': '#6ea8d8' }, ash: { '--bg': '#f0ede8', '--t1': '#1a1a18', '--ac': '#2e6da4' } };
  let theme = 'dusk';
  const root = { style: { setProperty(k, v) { inline[k] = v; }, removeProperty(k) { delete inline[k]; } }, setAttribute(k, v) { if (k === 'data-theme') theme = v; }, getAttribute(k) { return k === 'data-theme' ? theme : null; } };
  const ctx = { document: { documentElement: root }, window: {}, getComputedStyle: () => ({ getPropertyValue: (k) => (k in inline ? inline[k] : (SHEET[theme][k] || '')) }) };
  vm.createContext(ctx);
  vm.runInContext('var _current = null, _hookedSetTheme = false; function _writeCache(){} function _deriveOnAccent(){ return "#fff"; }\n' + UI.slice(a, c) + '\nthis.setLocal = setThemeLocal;', ctx);
  ctx.setLocal('dusk', { '--bg': '#0e0f12', '--t1': '#d4dae4', '--ac': '#6ea8d8' });   // a switch with vars: inline
  ctx.setLocal('ash');                                                                // then one by name alone
  t('a theme switched by name alone changes over (the old inline values are lifted first)', inline['--bg'] === '#f0ede8' && inline['--t1'] === '#1a1a18' && inline['--acc'] === '#2e6da4', JSON.stringify(inline));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
