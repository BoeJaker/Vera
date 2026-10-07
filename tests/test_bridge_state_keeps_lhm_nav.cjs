// 2026-09-28 (owner): "there is a bug in the chat ui the context lhm doesnt reliably load in"
// The chat frame sends the harness TWO vera:panel:state messages: VeraLHM's (nav + the menu spec) and the panel bridge's
// DOM snapshot. The harness replaces its cached nav with whatever the latest state carries, and reads a state without nav
// as "this panel has no menu" - so when the snapshot landed last, the chat's menu frame was taken down. The snapshot now
// carries the menu's own nav (VeraLHM.navState), and both publishers build it from the one function.
//   node tests/test_bridge_state_keeps_lhm_nav.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the bridge's snapshot, run as it is: the function body lifted out by its braces
function lift(src, head){
  const a = src.indexOf(head); if (a < 0) return '';
  let i = src.indexOf('{', a), depth = 0;
  for (; i < src.length; i++){ if (src[i] === '{') depth++; else if (src[i] === '}' && --depth === 0) return src.slice(a, i + 1); }
  return '';
}
const SNAP = lift(BR, 'function _safeDOMState(){');
t('the snapshot function is found', !!SNAP);
function snapshot(win, navItems){
  const doc = { title: 'Vera — Chat', body: {}, activeElement: null, querySelectorAll: () => [], querySelector: () => null };
  const ctx = { window: win, document: doc, location: { href: 'https://x/chat_panel?only=chat&harness=yes', hash: '' } };
  vm.createContext(ctx);
  vm.runInContext('var _navItems = ' + JSON.stringify(navItems) + ', _navActiveId = "a"; function _navLhm(){ return null; }'
    + ' function _uiCatalog(){ return { buttons: [], inputs: [] }; } var _actionProviders = [], _panelActions = {};\n'
    + SNAP + '\nthis.snap = _safeDOMState;', ctx);
  return ctx.snap();
}
const NAV = { items: [{ id: 'context', label: 'Context' }], active: 'context', lhm: { title: 'Chat', menus: [] } };
let s;
try { s = snapshot({ VeraLHM: { navState: () => NAV } }, null); } catch (e) { s = { err: String(e) }; }
t('a page whose menu is VeraLHM\'s: the snapshot carries that menu\'s nav', s && s.nav === NAV, JSON.stringify(s && (s.err || s.nav)));
try { s = snapshot({ VeraLHM: { navState: () => null } }, null); } catch (e) { s = { err: String(e) }; }
t('a page that owns no embedded menu: still no nav', s && !s.err && !('nav' in s), JSON.stringify(s));
try { s = snapshot({}, null); } catch (e) { s = { err: String(e) }; }
t('a page without VeraLHM: no nav, no throw', s && !s.err && !('nav' in s), JSON.stringify(s));
try { s = snapshot({ VeraLHM: { navState: () => NAV } }, [{ id: 'x', label: 'X' }]); } catch (e) { s = { err: String(e) }; }
t('an explicit registerNav still wins', s && s.nav && s.nav.items[0].id === 'x', JSON.stringify(s && (s.err || s.nav)));

// the menu's own publish goes through the same function, so the two cannot disagree
t('VeraLHM exposes navState', /navState: navState/.test(LHM));
t('its own publish builds nav from navState', /function _publish\(force\)\{\n    var nav = navState\(\); if\(!nav\) return;/.test(LHM));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
