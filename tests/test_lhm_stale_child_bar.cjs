// 2026-10-03: a sweep of every docked menu item on prod found Fabric's Memory Graph bar (Session | Recent | All | Fit ...)
// still in the harness top bar on Loom, Schedule, Bus, Stats, Skills and WorldView. The page re-checks what it relays once
// a second, but its signature only covered children with a MENU; Memory Graph's frame has a bar and no menu, so hiding
// it changed nothing and the relay never ran again.
//   node tests/test_lhm_stale_child_bar.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const VP = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const a = BR.indexOf('  function _kidShown('), b = BR.indexOf('  function _activeKid(');
const c = BR.indexOf('  function _barKidSig('), d = BR.indexOf('  setInterval(function(){ if(!_nestOn()) return; var k = _activeKid();');
t('the bar signature exists and sits beside the 1 s re-check', a > 0 && c > 0 && d > c);
const ctx = { String }; vm.createContext(ctx);
vm.runInContext('var _kids = [];\n' + BR.slice(a, b) + '\n' + BR.slice(c, d) + '\nthis.sig=_barKidSig; this.kids=_kids;', ctx);
const frame = (src) => ({ src, isConnected: true, offsetWidth: 800, offsetHeight: 600, getAttribute(k) { return k === 'src' ? this.src : null; } });
const graph = { frame: frame('/memgraph/panel'), hdr: { groups: [{ grp: 'g1' }] }, nav: null };   // a bar, no menu
ctx.kids.push(graph);
const shown = ctx.sig();
graph.frame.offsetWidth = 0; graph.frame.offsetHeight = 0;   // Fabric moves to Loom: the section holding the frame hides
const hidden = ctx.sig();
t('showing then hiding a child with a bar and no menu changes the signature', shown === '/memgraph/panel' && hidden === '' , shown + ' -> ' + hidden);
t('the 1 s re-check folds it in, so the relay runs again', /\+ '\|bar:' \+ _barKidSig\(\); if\(sig !== _kidSig\)\{ _kidSig = sig; publishStateDebounced\(\); _hdrRelay\(\); \}/.test(BR));
t('the shell never takes data-desc / data-desc-long as an item\'s id', /\/\^data-\(w\|tip\|title\|label\|desc\|i18n\|vera-\|rcm-\|icon\|lhm-\|vpb-\)\/\.test\(nm\)/.test(VP));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
