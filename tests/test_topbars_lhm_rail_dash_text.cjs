// 2026-09-27 (owner): top bars absorbed like the chat's · the inner menu's redundant ☰, rail icons level with their items ·
// larger widget text on dashboards, chips that fit at every text size · a composite's lists take whole rows
//   node tests/test_topbars_lhm_rail_dash_text.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const L = R('vera/chat/vera-lhm.js'), BR = R('vera/chat/vera-panel-bridge.js'), H = R('vera/capability_orchestration.html'), WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a docked menu has no ☰ unless asked; a spacer keeps the rail level', /if\(opts\.topIcon\)\{ var top = _el\('div', 'lhm-ico top'/.test(L) && /else rail\.appendChild\(_el\('div', 'lhm-rsp'\)\);/.test(L));
t('one rhythm: title 28, item 34, gap 2', /\.lhm-absorbed \.lhm-rsp\{height:28px;flex:0 0 28px\}/.test(L) && /\.lhm-absorbed \.lhm-tab\{height:34px;box-sizing:border-box;line-height:20px\}/.test(L) && /\.lhm-absorbed \.lhm-tabs \.lhm-ttl\{height:28px/.test(L));
t('the bridge knows more top-bar names', /\.pane-tb, #tb, #toolbar, \.toolbar, \.bar, header, \.page-head, \.panel-head';/.test(BR));
t('the harness brings the bridge to a panel that lacks it, then inits it', /function _bridgeInto\(f\)\{/.test(H) && /s\.src = '\/ui\/vera-panel-bridge\.js'/.test(H) && /document\.addEventListener\('load', e => \{ const t = e\.target; if\(t && t\.tagName === 'IFRAME'\) _bridgeInto\(t\); \}, true\);/.test(H));
t('dashboards: a higher text floor at every setting', /\.dash-grid vera-widget\{--vw-fmin:12\.5px\}/.test(WE) && /html\[data-text="larger"\] \.dash-grid vera-widget\{--vw-fmin:14\.5px\}/.test(WE));
t('the element measures its text scale; pills and log lines fit with it', /textK: textKOf\(this\)/.test(WE) && /const pillsFit = \(labels, H, W, k\) =>/.test(WE) && /Math\.floor\(H \/ \(15 \* \(\(o && o\.textK\) \|\| 1\)\)\)/.test(WE));
// behaviour: a composite of a figure, a list and a chart - the list takes a whole row, below the packed ones
{
  const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
  ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
  vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
  const rec = { form: 'composite', title: 'x', children: [{ slot: 'a', record: { form: 'rows', title: 'list', data: [{ name: 'a', v: 1 }] } }, { slot: 'b', record: { form: 'counter', title: 'n', data: { value: 3 } } }, { slot: 'c', record: { form: 'trace', title: 't', data: [1, 2, 3] } }] };
  const h = W.draw('composite', {}, 'l', { record: rec, sample: false, width: 600, height: 300 });
  const order = [...h.matchAll(/data-slot="([a-z])"([^>]*)>/g)].map((m) => m[1] + (/grid-column:span \d/.test(m[2]) ? '*' : ''));
  t('the list takes a whole row, after the packed figure and chart', order.join(',') === 'b*,c,a*' && /data-slot="a" style="[^"]*grid-column:span 3/.test(h));
  const p = W.draw('pills', [{ name: 'alpha-long-name', status: 'ok' }, { name: 'beta-long-name', status: 'ok' }, { name: 'gamma-long-name', status: 'ok' }, { name: 'delta-long-name', status: 'ok' }], 'm', { sample: false, width: 260, height: 30, textK: 1.5 });
  t('pills at a large text scale fold the rest into + N instead of cutting a row', /class="more"/.test(p));
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
