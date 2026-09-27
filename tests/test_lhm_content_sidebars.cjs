// 2026-09-27: a sidebar that carries content (the Calendar's) docks its tabs and keeps its content; the menu is the tab
// strip, never its panes; the children hear the page's real docked state; labels are clean
//   node tests/test_lhm_content_sidebars.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const PJ = R('vera/vera-panel.js'), CSS = R('vera/vera-panel.css'), BR = R('vera/chat/vera-panel-bridge.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a tab strip\'s buttons are the menu (data-t), never its panes', /\[data-t\]';/.test(PJ) && /b\.matches\('button, a, \[role="button"\], \[onclick\]'\)/.test(PJ) && /'data-k', 'data-t'\];/.test(PJ));
t('a content sidebar is marked', /host\.classList\.add\('vp-has-content'\)/.test(PJ));
t('docked, it keeps its content and folds its header and tabs', /html\.vpb-nav-hosted #sidebar\[data-vera-lhm\]\.vp-has-content \{ display: flex; \}/.test(CSS) && /\.vp-has-content > :is\(#side-head, #nav, #lhm-tabs, \.lhm-tabs\) \{ display: none; \}/.test(CSS));
t('children hear the page\'s real docked state', /\(_hostedUp \|\| document\.documentElement\.classList\.contains\('vpb-nav-hosted'\)\)/.test(BR));
t('labels are clean', (BR.match(/\.replace\(\/\\s\+\/g, ' '\)\.trim\(\)\.slice\(0, 48\)/g) || []).length >= 2);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
