// 2026-10-03 (owner), the LHM audit, point 1: "each panel use a common lhm styling that matches the lhm of the harness".
// Docked, every menu is drawn by the harness. Standalone, the menus Notebook, IDE and Workspaces gained for point 7 each
// styled themselves; they now take one look (.vp-menu) from the shared panel script, with the canonical sidebar's values.
//   node tests/test_lhm_shared_menu_look.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const CSS = R('vera', 'vera-panel.css'), VP = R('vera', 'vera-panel.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// run the injector against a stand-in document and read back the rules it adds
const added = []; let present = true;
const doc = { querySelector: (s) => (s === '.vp-menu' && present ? {} : null), getElementById: () => null, createElement: () => ({ set textContent(v) { this._t = v; }, get textContent() { return this._t; } }), head: { appendChild: (e) => added.push(e) } };
const a = VP.indexOf('  var VPM_BG1 = '), b = VP.indexOf('  function init() {');
const ctx = { document: doc }; vm.createContext(ctx); vm.runInContext(VP.slice(a, b) + '\nthis.run=initMenuLook;', ctx);
ctx.run(); const injected = (added[0] && added[0].textContent) || '';
t('a page with a .vp-menu gets the shared look injected', !!injected && /\.vp-menu \.nav-btn\{/.test(injected));
added.length = 0; present = false; ctx.run();
t('a page without one gets nothing', added.length === 0);

// the item look equals the canonical sidebar's, value by value
const canon = (CSS.match(/#sidebar\[data-vera-lhm\] \.nav-btn \{([^}]*)\}/) || [])[1] || '';
const item = (injected.match(/\.vp-menu \.nav-btn\{([^}]*)\}/) || [])[1] || '';
const norm = (s) => s.replace(/\s+/g, '').toLowerCase();
for (const k of ['display:flex', 'align-items:center', 'gap:9px', 'width:100%', 'padding:8px10px', 'border:1pxsolidtransparent', 'border-radius:var(--ui-radius,5px)', 'font-size:11px', 'margin-bottom:2px', 'white-space:nowrap', 'transition:background.12s,color.12s,border-color.12s']) {
  t('item ' + k + ' - same as the canonical sidebar', norm(canon).includes(k) && norm(item).includes(k), 'canon has: ' + norm(canon).includes(k) + ', shared has: ' + norm(item).includes(k));
}
t('glyph box and svg size match too', /#sidebar\[data-vera-lhm\] \.nav-btn \.gl \{ width: 15px;[^}]*font-size: 13px; \}/.test(CSS) && /\.vp-menu \.nav-btn \.gl\{width:15px;[^}]*font-size:13px\}/.test(injected) && /\.vp-menu \.nav-btn svg\{width:14px;height:14px;/.test(injected));
t('the column is the canonical width, and an inline menu is a block', /\.vp-menu\{width:var\(--lhm-width,184px\);/.test(injected) && /--lhm-width: 184px;/.test(CSS) && /\.vp-menu\.inline\{width:auto;border-right:none;/.test(injected));
t('colours try the canonical names first, then the page\'s own', /var\(--acc, var\(--accent, var\(--ac, #5a9e8f\)\)\)/.test(VP) && /var\(--bg2, var\(--s2, #1a1f26\)\)/.test(VP));
t('docked, the harness draws the menu instead', /html\.vpb-nav-hosted \.vp-menu\{display:none!important\}/.test(injected));
t('the shared look is injected first, before the menu is read', /function init\(\) \{ initMenuLook\(\); initCollapse\(\);/.test(VP));

// the three pages: on the shared look, no menu styling of their own, the shared script loaded
for (const [name, file, id, cls] of [['Notebook', ['vera', 'research', 'notebook_panel.html'], 'nb-views', 'vp-menu inline'], ['IDE', ['vera', 'ide', 'vscode_panel.html'], 'views', 'vp-menu'], ['Workspaces', ['vera', 'remote', 'workspace_panel.html'], 'wsViews', 'vp-menu']]) {
  const H = R(...file);
  const style = (H.match(/<style[\s\S]*?<\/style>/g) || []).join('\n');
  t(name + ': its menu is a ' + cls, new RegExp('<nav id="' + id + '" class="' + cls + '" data-vera-lhm').test(H));
  t(name + ': no menu styling of its own', !new RegExp('#' + id + '\\s*(\\.nav-btn|\\.gl|\\{)').test(style));
  t(name + ': loads the shared panel script', /<script src="\/ui\/vera-panel\.js"><\/script>/.test(H));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
