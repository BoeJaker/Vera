// 2026-09-27: a menu item's glyph (data-icon, unique per item) is never its id - Research's items published as '⌕' and a
// pick of nv-p reached nothing
//   node tests/test_nav_id_not_icon.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const PJ = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
const ok = /\/\^data-\(w\|tip\|title\|label\|i18n\|vera-\|rcm-\|icon\|lhm-\)\//.test(PJ);
console.log((ok ? 'ok   ' : 'FAIL ') + 'data-icon and data-lhm-* are never an id');
process.exit(ok ? 0 : 1);
