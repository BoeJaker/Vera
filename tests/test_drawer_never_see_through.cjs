// 2026-09-28 (owner): "the details sidepanel that pops out is partially transparent on lots of pages not just the observe ui".
// Its ground was opaque wherever measured; its own opacity was not - the slide-in faded it up from 0. It slides without fading.
//   node tests/test_drawer_never_see_through.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), DS = R('vera/ui/design.css');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const css = (WE.match(/const DRAWER_CSS = '([^\n]*)/) || [])[1] || '';
const kf = (css.match(/@keyframes vw-drin\{[^}]*\}\}/) || [''])[0];
t('the drawer slides in on a transform alone - no opacity in its entrance', /translateX\(24px\)/.test(kf) && !/opacity/.test(kf));
t('the drawer itself never sets an opacity or a translucent ground', /\.vw-drawer\{position:fixed;[^}]*background:var\(--s1,var\(--bg1,#15171c\)\)/.test(css) && !/\.vw-drawer\{[^}]*opacity:/.test(css));
t('blocks off still leaves the drawer its ground', /\.vw-tip, \.vw-drawer, \.cmenu/.test(DS));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
