// 2026-10-03 (owner), the LHM audit, point 7: sub-UIs belong in the LHM; top bars are for settings and status. Probed on
// prod: the Models tab's top bar carried Nodes | Jobs | Catalog | Benchmark | Traffic - the Ollama pane's own sections -
// beside its docked menu. A page's sections (data-vera-sub) were already listed in the docked menu under the open item;
// they now leave the bar the harness absorbs whenever that menu is docked, and carry their descriptions into it.
//   node tests/test_lhm_subs_out_of_topbar.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const EST = fs.readFileSync(path.join(__dirname, '..', 'vera', 'workers', 'workers_ollama_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the bridge ─────────────────────────────────────────────────────────────────────────────────────────────────────────
t('docked, the bar offered to the harness drops the page\'s sections', /var subsDocked = false; try\{ subsDocked = document\.documentElement\.classList\.contains\('vpb-nav-hosted'\) && !!\(_navItems && _navItems\.length\); \}catch\(e\)\{\}/.test(BR)
  && /return out\.filter\(function\(el\)\{ return _hdrShownIn\(el, bar\) && !\(subsDocked && el\.matches && el\.matches\('\[data-vera-sub\]'\)\); \}\);/.test(BR));
t('undocked (standalone, or no menu to dock) they stay in the bar', /subsDocked = document\.documentElement\.classList\.contains\('vpb-nav-hosted'\) && !!\(_navItems && _navItems\.length\)/.test(BR));
t('the bar is offered again the moment docking changes', /_kidsHosted\(true\);\n      _hdrReoffer\(\);/.test(BR) && /_kidsHosted\(false\);\n      _hdrReoffer\(\);/.test(BR) && /function _hdrReoffer\(\)\{ try\{ if\(_hdrBar\)\{ _hdrSig = ''; setTimeout\(function\(\)\{ try\{ _hdrOffer\(true\); \}catch\(e\)\{\} \}, 0\); \} \}catch\(e\)\{\} \}/.test(BR));
// the sections carry their descriptions into the docked menu: run _pageSubs + _navLhm on stand-in elements
{
  const a = BR.indexOf('  function _subShown('), b = BR.indexOf('  function _kidsHosted(');
  const c = BR.indexOf('  var _navLhmOpts = null;'), d = BR.indexOf('  // ── DOM helpers');
  const el = (id, label, on, desc, long) => ({ attrs: { 'data-vera-sub': id, 'data-desc': desc, 'data-desc-long': long }, className: on ? 'btn ol-sub active' : 'btn ol-sub', hidden: false, childNodes: [{ nodeType: 3, textContent: label }], textContent: label, parentElement: null,
    getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; }, hasAttribute(k) { return k in this.attrs; } });
  const subs = [el('ol-nodes', 'Nodes', true, 'The Ollama nodes', 'Every node, long'), el('ol-traffic', 'Traffic', false, 'Who calls each node', 'Traffic, long')];
  const ctx = { document: { title: 'Vera — Models', body: {}, querySelectorAll: () => subs, querySelector: () => null }, getComputedStyle: () => ({ visibility: 'visible', display: 'block' }), String, Array };
  vm.createContext(ctx);
  vm.runInContext('var _navItems = null, _navActiveId = ""; function _shownKidCta(){ return null; }  /* no nested panel shown: the nested primary action is tested in test_ui_bootstrap */ function _activeKid(){ return null; }\n' + BR.slice(a, b) + '\n' + BR.slice(c, d) + '\nthis.lhm=_navLhm; this.set=function(i,x){ _navItems=i; _navActiveId=x; };', ctx);
  ctx.set([{ id: 'ollama', label: 'Ollama' }, { id: 'routing', label: 'Model Routing' }], 'ollama');
  const s = ctx.lhm(); const m = s.menus.find((x) => x.id === s.active.menu);
  const sub = m.tabs.filter((x) => String(x.id).indexOf('s:') === 0);
  t('the sections sit under the open item in the docked menu', sub.map((x) => x.id).join(',') === 's:ol-nodes,s:ol-traffic' && s.active.tab === 's:ol-nodes', JSON.stringify(m.tabs.map((x) => x.id)));
  t('each carries its short and long description', sub[1].desc === 'Who calls each node' && sub[1].desc_long === 'Traffic, long', JSON.stringify(sub[1]));
}

// ── the Estate / Models page: its panes' sections are marked ──────────────────────────────────────────────────────
const ol = ['nodes', 'jobs', 'catalog', 'benchmark', 'wrapper'];
t('the Ollama pane\'s five sections are page sections, described', ol.every((s) => new RegExp('data-olsub="' + s + '" data-vera-sub="ol-[a-z]+"(?: data-label="[^"]+")? data-desc="[^"]{8,}" data-desc-long="[^"]{30,}"').test(EST)));
t('the workers pane\'s two as well', ['nodes', 'jobs'].every((s) => new RegExp('data-wksub="' + s + '" data-vera-sub="wk-' + s + '" data-desc="[^"]{8,}" data-desc-long="[^"]{30,}"').test(EST)));
t('Catalog keeps its download badge; Traffic is named Traffic in the menu', /data-vera-sub="ol-catalog" data-label="Catalog"[^>]*>Catalog<span id="ol-dl-badge"/.test(EST) && /data-vera-sub="ol-traffic" data-label="Traffic"/.test(EST));
const ids = (EST.match(/data-vera-sub="([^"]+)"/g) || []).map((x) => x.slice(15, -1));
t('no two sections share an id', new Set(ids).size === ids.length, ids.filter((x, i) => ids.indexOf(x) !== i).join(','));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
