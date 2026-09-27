// 2026-09-27 (owner): "the comms and estate storage menus and any other deep LHMs needs fully absorbing into the new
// chat/harness ui based unified LHM system"
//   node tests/test_lhm_nested_panels.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('only a panel in the harness nests (not the harness, not the chat)', /function _nestOn\(\)\{ try\{ return window\.parent && window\.parent !== window && !document\.documentElement\.hasAttribute\('data-harness'\);/.test(BR) && /if\(!_nestOn\(\)\) return;\n    var d = ev\.data;/.test(BR));
t('a panel hears the menus its child panels publish', /var k = _kidOf\(ev\.source\); if\(!k\) return;/.test(BR) && /k\.nav = \(nv && Array\.isArray\(nv\.items\)\)/.test(BR));
t('docked here is docked for the children (their sidebars fold)', /_kidsHosted\(true\);/.test(BR) && /_kidsHosted\(false\);/.test(BR));
t('a pick on a nested section goes down to the child', /if\(id\.indexOf\('c:'\) === 0\)\{ var kid = _activeKid\(\);/.test(BR) && /action: 'nav_select', action_id: 'nest-'/.test(BR));
t('a container with no bar offers the shown child\'s', /function _hdrRelay\(\)\{/.test(BR) && /_hdrKid\.win\.postMessage\(d, '\*'\)/.test(BR));
t('nested items render indented in the docked menu', /\(t\.depth \? ' sub' : ''\)/.test(LHM) && /\.lhm-absorbed \.lhm-tab\.sub\{padding-left:24px/.test(LHM));
// the spec: Comms, Calendar shown, its sections nested under Calendar with its current one lit
{
  const a = BR.indexOf('  var _navLhmOpts = null;'), b = BR.indexOf('  // ── DOM helpers');
  const ctx = { document: { title: 'Vera — Comms' } }; vm.createContext(ctx);
  vm.runInContext('var _navItems = null, _navActiveId = ""; var _kid = null; function _activeKid(){ return _kid; }\n' + BR.slice(a, b) + '\nthis.lhm=_navLhm; this.set=function(i,a,k){ _navItems=i; _navActiveId=a; _kid=k; };', ctx);
  ctx.set([{ id: 'system', label: 'System' }, { id: 'calendar', label: 'Calendar' }, { id: 'email', label: 'Email' }], 'calendar',
          { nav: { items: [{ id: 'month', label: 'Month' }, { id: 'agenda', label: 'Agenda' }], active: 'agenda' } });
  const s = ctx.lhm(); const m = s.menus.find((x) => x.id === s.active.menu);
  t('the child\'s sections sit under the item that shows it', m && m.tabs.map((x) => x.id).join(',') === 'system,calendar,c:month,c:agenda,email', m && m.tabs.map((x) => x.id).join(','));
  t('its current section is lit', s.active.tab === 'c:agenda', s.active.tab);
}
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
