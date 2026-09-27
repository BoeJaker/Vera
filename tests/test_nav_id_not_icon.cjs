// 2026-09-27: a menu item's glyph (data-icon, unique per item) is never its id - Research's items published as '⌕' and a
// pick of nv-p reached nothing
//   node tests/test_nav_id_not_icon.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const PJ = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
const ok = /\/\^data-\(w\|tip\|title\|label\|i18n\|vera-\|rcm-\|icon\|lhm-\|vpb-\)\//.test(PJ)
  && PJ.indexOf("if (ok3) idAttr = 'id';") < PJ.indexOf('var at0 = btns[0].attributes');   // the element id before any data attribute
console.log((ok ? 'ok   ' : 'FAIL ') + 'data-icon, data-lhm-* and the bridge\'s data-vpb-* are never an id; an element id comes first');
process.exit(ok ? 0 : 1);
