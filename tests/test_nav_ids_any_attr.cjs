// 2026-09-27: every panel's menu items have ids and clean labels, whatever attribute its buttons use (the Estate's
// Storage: data-p) - "the comms and estate storage menus ... fully absorbing into the unified LHM"
//   node tests/test_nav_ids_any_attr.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const PJ = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('any data-* attribute that names every item apart is the id', /if \(!idAttr\) \{\n\s+var at0 = btns\[0\]\.attributes \|\| \[\];/.test(PJ) && /if \(ok\) idAttr = nm;/.test(PJ));
t('failing that, the item\'s place - an id a pick can always reach', /\('n' \+ Array\.prototype\.indexOf\.call\(btns, b\)\)/.test(PJ));
t('a label beside its .gl glyph is read without the glyph', /var gl = !txt && b\.querySelector\('\.gl'\);/.test(PJ));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
