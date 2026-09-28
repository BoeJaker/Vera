// 2026-09-28 (owner): "themes are not being applied properly" to the chat - a light theme from the harness's swatches left the chat
// frame color-scheme:dark, and the browser painted its dark canvas behind it
//   node tests/test_theme_color_scheme.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const UI = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-ui.js'), 'utf8');
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
t('applyVars sets the page colour-scheme from the theme background, recorded with the vars', UI.includes("if(schemeBg){ var sl = _schemeLum(schemeBg); if(sl !== null) set('color-scheme', sl > 0.5 ? 'light' : 'dark'); }"));
const src = UI.slice(UI.indexOf('  function _schemeLum(c){'), UI.indexOf('  function applyVars(vars){'));
const lum = new Function(src + '; return _schemeLum;')();
t('light and dark theme backgrounds read the right way (ash, paperwhite light; dusk, matrix, chalk dark)', lum('#f0ede8') > 0.5 && lum('#ffffff') > 0.5 && lum('#0e0f12') < 0.5 && lum('#020a02') < 0.5 && lum('#1c1c1e') < 0.5 && lum('rgb(240, 237, 232)') > 0.5 && lum('#fff') > 0.5 && lum('var(--x)') === null);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
