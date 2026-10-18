// SM-4: an iso form named in full (gallery@iso) draws its base form's sample - it fell to the STRING sample, so every
// x@iso form showed its empty message ("a gallery needs items") with its own sample data in the Desk Lab.
//   node tests/test_widget_iso_sample.cjs
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(fs.readFileSync(path.join(here, '..', 'vera', 'ui', 'iso.js'), 'utf8'), ctx);
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const ISO = [...new Set([...src.matchAll(/R\['([a-z-]+)@iso'\] = /g)].map((m) => m[1]))];
t('the iso forms are found (' + ISO.length + ')', ISO.length >= 20);
const same = ISO.filter((b) => JSON.stringify(W.sample(b + '@iso')) === JSON.stringify(W.sample(b)));
t('an @iso form\'s sample is its base form\'s', same.length === ISO.length, ISO.filter((b) => !same.includes(b)).join(','));
t('the gallery\'s is rows, not text', Array.isArray(W.sample('gallery@iso')) && W.sample('gallery@iso')[0].name === 'render 04');
const empty = ISO.filter((b) => { const h = String(W.draw(b + '@iso', W.sample(b + '@iso'), 'm', { title: b }) || ''); return /needs |no rows|need OHLC|need values|need series/.test(h) && /wempty/.test(h); });
t('no iso form draws its empty message with its own sample', !empty.length, empty.join(','));
t('a plain sample is unchanged', typeof W.sample('string') === 'string' && W.sample('files')[0].path === '/srv/vera/fabric.py');
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
