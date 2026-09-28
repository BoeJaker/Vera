// 2026-09-28: a tile's size reads its span against the grid's own width (a dashboard in a narrow pane draws M, not XL)
//   node tests/test_dash_size_by_width.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const VD = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the span is read against the grid\'s width', /function effSpan\(sp\) \{ var gw = grid\.clientWidth \|\| 0, k = gw \? Math\.min\(1, gw \/ 1200\) : 1;/.test(VD) && (VD.match(/effSpan\(/g) || []).length >= 4 && VD.includes("el.setAttribute('size', sizeForSpan(sp0[0], sp0[1]))"));
t('the sizes follow the grid\'s width', /new ResizeObserver\(function \(\) \{ var gw = grid\.clientWidth/.test(VD));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
