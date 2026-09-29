// 2026-09-27 (owner): "the research ui lhm needs integrating into the unified lhm and im sure there are more that are missing"
//   node tests/test_any_menu_docks.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const PJ = R('vera/vera-panel.js'), BR = R('vera/chat/vera-panel-bridge.js'), RS = R('vera/research/research_panel.html'), CO = R('vera/ontologies/cap_ontology_panel.html');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a menu in markup of its own: its named clickable things are the items, never what is skipped', /b\.closest\('\[data-lhm-skip\]'\)/.test(PJ));
t('an item\'s own element id is its id when every item has one', /if \(ok3\) idAttr = 'id';/.test(PJ));
t('data-icon is the item\'s glyph', /b\.getAttribute\('data-icon'\)/.test(PJ));
t('the bridge brings the nav code to a page that marks a menu, and folds it firmly while docked', /s\.src = '\/ui\/vera-panel\.js'/.test(BR) && /html\.vpb-nav-hosted \[data-vera-lhm\]:not\(\.vp-has-content\)\{display:none!important\}/.test(BR));
t('Research: the rail docks, glyphs, the palette skipped, one column while docked', /<nav id="nav" data-vera-lhm>/.test(RS) && /<div id="pal" data-lhm-skip>/.test(RS) && /id="nv-r" data-icon=/.test(RS) && /html\.vpb-nav-hosted #shell\{grid-template-columns:1fr\}/.test(RS));
t('Cap Ontology: its view strip docks', /<div class="view-tabs" data-vera-lhm>/.test(CO) && /id="vt-graph" title="Graph" data-icon="◉"/.test(CO));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
