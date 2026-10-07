// 2026-09-28 (owner): "i think its possible for 2 burger menus to be at the top left of the lhm make sure there is only one"
// The top of the harness's menu carries ONE ☰, and it only swaps between the open panel's menu and the every-tab list.
// The list's header used to draw a ☰ that switched to horizontal tabs - a second burger meaning a second thing, and the
// one the user met whenever the chat's menu had dropped out. Horizontal tabs are the foot's ☰ (.lhm-nav-foot).
//   node tests/test_lhm_one_burger.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('side() draws its header ☰ only when there is something to swap to',
  /if\(cfg\.top\.toggle\)\{ var tb = _el\('button', 'lhm-s-tb'/.test(LHM));
t('the harness list header has no tabs toggle', !/top: _lhmTopBack\(\) \|\| \{[^}]*tabToggleLhm/.test(H));
t('the list header still swaps back to the open panel\'s menu', /top: _lhmTopBack\(\) \|\| \{/.test(H) && /toggleTitle: 'Back to ' \+ name/.test(H));
t('horizontal tabs stay one press away, in the foot', /<div class="lhm-nav-foot">\s*<button class="tab-ctrl-btn" onclick="tabToggleLhm\(\)"/.test(H));
t('swapped to the list, the absorb path draws no bar of its own (no second ☰ above the header)',
  /function _lhmRenderAbsorbed\(host, pid, nav\)\{\n  if\(_lhmAbsorbTop\) return false;/.test(H));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
