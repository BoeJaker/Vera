// 2026-09-27: a figure fits its cell - "1,05..." in the fabric's size tile
//   node tests/test_numbers_fit.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const WE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const vm = require('node:vm');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('each figure cell is a container; the figure sizes to it', /\.vb-bigs div\{display:flex;flex-direction:column;gap:1px;min-width:0;container-type:inline-size\}/.test(WE) && /font-size:clamp\(13px, min\(30cqh, 16cqi\), 30px\)/.test(WE));
t('a count of a million or more, three or more a row, is said short', /const fig = \(v\) => \(perRow >= 3 && Math\.abs\(num\(v\)\) >= 1e6\) \? shortN\(v\) : fmt\(v\);/.test(WE));
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
if (W && W.draw) { const h = W.draw('numbers', { records: 1058218, datasets: 5549, sources: 2106, feeds: 12 }, 'l', { sample: false, width: 560 }); t('drawn: 1.06M, and the item keeps 1058218', /1\.06M/.test(h) && /1058218/.test(h)); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
