// 2026-09-28: a tile's size reads its span against the grid's own width (a dashboard in a narrow pane draws M, not XL)
//   node tests/test_dash_size_by_width.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const VD = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-dashboard.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
/* WSU-43 (2026-10-11): against the grid's OWN columns x 100 px - under 1100 px the grid is 6 (or 2) columns already, and
   width/1200 shrank the span twice (a nested section grid at 388 px drew 4-wide tiles as xs). Run, not matched: */
{ const src = VD.match(/function effSpan\(sp\) \{[^\n]*\n[^\n]*\}/);
  const mk = (gw, cols) => { const grid = { clientWidth: gw }; const getComputedStyle = () => ({ gridTemplateColumns: Array(cols).fill('50px').join(' ') }); const GRID = { cols: 12 };
    return eval('(' + (src ? src[0] : 'function(){ return [0, 0]; }') + ')'); };
  t('twelve columns: as before (1200 px keeps the span, 600 px halves it)', !!src && mk(1200, 12)([4, 6])[0] === 4 && mk(600, 12)([4, 6])[0] === 2 && mk(1600, 12)([6, 3])[0] === 6);
  t('six columns in 388 px: a 4-wide tile stays M, never the xs chip', mk(388, 6)([4, 6])[0] === 3 && mk(388, 6)([3, 4])[0] === 2);
  t('an unmeasured grid keeps the span', mk(0, 6)([4, 6])[0] === 4); }
t('the span is read against the grid\'s width', /function effSpan\(sp\) \{ var gw = grid\.clientWidth \|\| 0,/.test(VD) && (VD.match(/effSpan\(/g) || []).length >= 4 && VD.includes("el.setAttribute('size', sizeForSpan(sp0[0], sp0[1]))"));
t('the sizes follow the grid\'s width', /new ResizeObserver\(function \(\) \{ var gw = grid\.clientWidth/.test(VD));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
