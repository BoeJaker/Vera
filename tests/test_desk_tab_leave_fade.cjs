// WSU-96 remainder: a card that goes behind a combined card's tab fades out (cmp-leaving, ~200 ms) instead of vanishing;
// a card that was not in view (or is new) is hidden at once as before. Real-page check: tools/snip_tab_fade.js.
'use strict';
const fs = require('fs'); const path = require('path');
const S = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let n = 0, bad = 0; const t = (name, ok) => { n++; if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
t('a card in view the pass before gets cmp-leaving when it goes behind', /res\.behind\.add\(c\); c\.classList\.add\('cmp-behind'\);[\s\S]{0,700}if \(this\._shownLast && this\._shownLast\.has\(c\.dataset\.key\) && !c\.classList\.contains\('desk-new'\)\) \{ c\.classList\.add\('cmp-leaving'\)/.test(S));
t('cmp-leaving is taken off after the fade', /c\._clT = setTimeout\(\(\) => c\.classList\.remove\('cmp-leaving'\), 200\)/.test(S));
t('while leaving it stays drawn and fades, and takes no input', S.includes('.stage .it.cmp-behind.cmp-leaving:not(.cmp-front){display:flex!important;opacity:0;transition:opacity .18s;pointer-events:none}'));
t('a hidden tab is still hidden once it has faded', S.includes('.stage .it.cmp-behind:not(.cmp-front){display:none!important}'));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
