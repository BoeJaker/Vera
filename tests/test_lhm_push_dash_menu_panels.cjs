// 2026-09-27 (owner): "the chat lhm needs to push and squeeze the other elements on the x axis so its not open over the top of
// them - same for the menu in the harness and all of the other panels too" · "can the harness dashboard have the same
// background and menu setup as the chat ui and can each internal ui panel drop into its lhm menu like the chat"
//   node tests/test_lhm_push_dash_menu_panels.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const PJ = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── the push ────────────────────────────────────────────────────────────────────────────────────────────────────
t('auto-hide keeps the menu in the row', /\.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide\{position:relative;top:auto;left:auto;bottom:auto;transform:none;opacity:1;pointer-events:auto;flex-shrink:0/.test(HAR));
t('so the page keeps no gutter for it (it moves aside with the menu instead)', /\.body-wrap\.lhm \.main\.ah-rail,[^{]*\.body-wrap\.lhm \.main\.autohide-reserve-v\.absorbed\.chatmenu,[^{]*\{padding-left:0!important\}/.test(HAR));
t('and every change of the menu\'s width eases', /\.body-wrap\.lhm #lhmNav\.lhm-nav\{transition:width \.34s/.test(HAR));
t('the reveal no longer floats with a shadow', !/autohide\.show\{box-shadow:0 18px 50px/.test(HAR));

// ── the dashboard ───────────────────────────────────────────────────────────────────────────────────────────────
t('the dashboard has the chat\'s ground', /body\.dash-open #veraWashH\{opacity:1\}/.test(HAR) && /body\.dash-open \.body-wrap\{position:relative;z-index:1;background:transparent\}/.test(HAR) && /body\.dash-open #lhmNav\.lhm-nav\{background:color-mix/.test(HAR));
t('known open from the tab', /document\.body\.classList\.toggle\('dash-open', pid === 'dashboard'\);/.test(HAR));
t('its menu is published as an LHM spec and drawn by the same absorb', /_panelNavCache\.dashboard = nav; _lhmNavSync\(\);/.test(HAR) && /return \{ title: 'Dashboard', active: act, menus, open: \[\] \};/.test(HAR));
t('its picks go to its own handler, not a frame', /if\(_localLhm\[pid\]\)\{ _localLhm\[pid\]\(itemId\); return; \}/.test(HAR) && /_localLhm\.dashboard = function\(id\)\{/.test(HAR));
t('layouts, sections, widgets, arrange, live', ['layouts', 'sections', 'widgets', 'arrange', 'live'].every((k) => HAR.includes("{ id: '" + k + "', icon: ")));
t('the dashboard menu is declared after _dashCtl (no temporal dead zone at boot)', HAR.indexOf('let _dashCtl=null;') > 0 && HAR.indexOf('(function _dashLhmMount(){') > HAR.indexOf('let _dashCtl=null;'));

// ── every panel ─────────────────────────────────────────────────────────────────────────────────────────────────
{
  const a = BR.indexOf('  var _navLhmOpts = null;'), b = BR.indexOf('  // ── DOM helpers');
  t('the bridge builds the spec', a > 0 && b > a);
  const ctx = { document: { title: 'Vera \u2014 Estate' } }; vm.createContext(ctx);
  vm.runInContext('var _navItems = null, _navActiveId = "";\n' + BR.slice(a, b) + '\nthis.lhm=_navLhm; this.res=_navResolve; this.set=function(i,a){ _navItems=i; _navActiveId=a; };', ctx);
  ctx.set([{ id: 'overview', label: 'Overview', group: 'Overview', icon: '\u25a6' }, { id: 'ops', label: 'Live ops', group: 'Overview', icon: '\u2726' }, { id: 'machines', label: 'All', group: 'Machines', icon: '\u25a4' }, { id: 'docker', label: 'Docker', group: 'Machines', icon: '\u25e7' }], 'docker');
  let s = ctx.lhm();
  t('grouped: a rail icon per group, its items as the list', s.menus.length === 2 && s.menus[1].label === 'Machines' && s.menus[1].tabs.length === 2 && s.menus[0].icon === '\u25a6', JSON.stringify(s.menus.map((m) => m.label)));
  t('the open item lights its group and itself', s.active.menu === '\u00a7g1' && s.active.tab === 'docker');
  t('titled with the panel\'s name, not its page title', s.title === 'Estate', s.title);
  t('a pick in the list selects that item', ctx.res('\u00a7g0/ops') === 'ops');
  t('a group icon stays on the open item when it is in the group, else its first', ctx.res('\u00a7g1') === 'docker' && ctx.res('\u00a7g0') === 'overview');
  t('a plain item id still selects as before', ctx.res('ops') === 'ops');
  ctx.set([{ id: 'datasets', label: 'Datasets', icon: '\u25eb' }, { id: 'sources', label: 'Sources' }], 'sources');
  s = ctx.lhm();
  t('flat: a rail icon per item (a letter when it has no glyph), the whole list beside it', s.menus.length === 2 && s.menus[1].icon === 'S' && s.menus[0].tabs.length === 2 && s.active.menu === '\u00a7i1');
  t('a flat icon selects its item', ctx.res('\u00a7i0') === 'datasets');
}
t('the bridge publishes it with the items', /st\.nav = \{items: _navItems, active: _navActiveId\}; var _lhm = _navLhm\(\); if\(_lhm\) st\.nav\.lhm = _lhm;/.test(BR));
t('and resolves picks before selecting', /id = _navResolve\(String\(id\)\);/.test(BR));
t('the panel shell gives each item its group and glyph', /return \{ id: idOf\(b\), label: labelOf\(b\), group: groupOf\(b\), icon: iconOf\(b\) \};/.test(PJ) && /el\.classList\.contains\('nav-grp'\)/.test(PJ));

const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
t('an absorbed menu\'s list reads at the menu\'s size', /'\.lhm-absorbed \.lhm-tab\{padding:7px 9px;[^']*font-size:11\.5px;/.test(LHM) && !/\.lhm-absorbed \.lhm-tab\{[^']*font-size:9\.5px/.test(LHM) && /e\.title = t\.label;/.test(LHM));
t('the dashboard rail\'s \u2630 gives the every-tab list', /if\(menu === '\\u2630'\)\{ _lhmAbsorbTop = true; _lhmNavSync\(\); return; \}/.test(HAR));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
