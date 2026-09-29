// 2026-09-27 (owner): "make the chat ui styling (with the glow background and blocks styling and zen, hover, full, themes,
// styles like pixel newspaper etc) to all of the ui panels and unify them all under one design approach"
//   node tests/test_one_design.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const D = R('vera/ui/design.css'), UI = R('vera/vera-ui.js'), LIBS = R('vera/ui/libs.py'), CHAT = R('vera/chat/chat_panel.html'), HAR = R('vera/capability_orchestration.html'), RCM = R('vera/ui/rcm.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('the pack\'s type reaches the panels\' own font names', /--font-ui: var\(--f-ui,/.test(D) && /--mono: var\(--font-mono\);/.test(D) && /html:root body \{ font-family: var\(--font-ui\); \}/.test(D));
t('the glow behind every page', /html:root body::before \{/.test(D) && /radial-gradient\(120% 100% at 10% 0%/.test(D));
t('blocks on: raised, rounded surfaces', /html:root:not\(\[data-blocks="off"\]\) :is\(\.card,/.test(D) && /box-shadow: var\(--elev\);/.test(D));
t('blocks off: no surfaces of their own, the sidebar too', /html:root\[data-blocks="off"\] :is\(\.card,/.test(D) && /html:root\[data-blocks="off"\] #sidebar\[data-vera-lhm\] \{ background: transparent;/.test(D));
t('Full / Hover / Zen', /html:root\[data-den="zen"\] body:not\(\.lhm-collapsed\) #sidebar\[data-vera-lhm\] \.nav-btn :is\(\.gl, svg\) \{ display: none; \}/.test(D) && /html:root\[data-den="hover"\]/.test(D));
t('served as css', /@APP\.get\("\/ui\/design\.css"/.test(LIBS) && /media_type="text\/css"/.test(LIBS));
t('every page loads it, but the chat and the harness draw the design themselves', /dl\.href = BASE \+ '\/ui\/design\.css';/.test(UI) && /hasAttribute\('data-design-own'\)/.test(UI) && /<html lang="en" data-design-own>/.test(CHAT) && /<html lang="en" data-design-own>/.test(HAR));
t('the menu: one row per action, remove only on a canvas', /'a:' \+ String\(r\.id \|\| r\.n\)/.test(RCM) && /r\.id === 'remove' && !x\.canRemove/.test(RCM));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
