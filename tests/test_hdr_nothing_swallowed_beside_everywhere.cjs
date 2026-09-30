// 2026-09-28 (owner): "the issue that happened with the perf ui... have any other parts of uis been swallowed/hidden by
// mistake?" and "in the LHM all the sub menus are supposed to let you open pages as extra panels side-by-side but this
// only seems to be the case in the estate panel - others are missing the feature?"
//   node tests/test_hdr_nothing_swallowed_beside_everywhere.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const BR = R('vera', 'chat', 'vera-panel-bridge.js'), LHM = R('vera', 'chat', 'vera-lhm.js'), VP = R('vera', 'vera-panel.js'), HAR = R('vera', 'capability_orchestration.html');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── 1. nothing in an absorbed bar is swallowed ──
const ctl = (BR.match(/var _HDR_CTL = '([^']+)'/) || [])[1] || '';
t('every clickable kind is offered: links, onclick, tabs, roles, tabindex, summary',
  ['a[href]', '[onclick]', '[role=tab]', '[role=button]', '[role=link]', '[role=menuitem]', '[data-vera-sub]', 'summary', '[tabindex]', 'button', 'select', 'input', 'textarea'].every((k) => ctl.includes(k)), ctl);
t('an element drawn with a pointer cursor (a click bound in script) is offered - the outermost one',
  /getComputedStyle\(el\)\.cursor === 'pointer' && \(!el\.parentElement \|\| getComputedStyle\(el\.parentElement\)\.cursor !== 'pointer'\)/.test(BR));
t('what sits inside a control is the control; a wrapper round a control gives way to it',
  /if\(_hdrNative\(n\) \|\| \(n\.tagName === 'A' && n\.hasAttribute\('href'\)\)\) return;/.test(BR) && /if\(out\[i\] !== el && el\.contains\(out\[i\]\)\) return false;/.test(BR));
t('the offer is built from that one list (not button/select/input only)',
  /var all = _hdrCtls\(bar\);/.test(BR) && /var ctl = all\.filter\(function\(el\)\{ return el === ch \|\| ch\.contains\(el\); \}\);/.test(BR) && !/ch\.querySelectorAll\('button, select, input'\)/.test(BR));
t('a tab goes up as a tab, lit when current; a link as a link', /tab: !!tab, link: !!lnk/.test(BR) && /aria-selected'\) === 'true'/.test(BR));
t('a group holds up to 40, not 16', /ctl\.slice\(0, 40\)/.test(BR) && !/ctl\.slice\(0, 16\)/.test(BR));
t('date / time / colour / slider inputs go up as themselves', /\^\(date\|time\|datetime-local\|month\|week\|color\|range\)\$/.test(BR) && /\^\(number\|date\|time\|datetime-local\|month\|week\|color\|range\)\$/.test(HAR));
t('the harness draws a tab proxy as a tab', /\(it\.tab \? ' hp-tab' : ''\)/.test(HAR) && /el\.setAttribute\('aria-selected', it\.on \? 'true' : 'false'\)/.test(HAR));

// ── 2. the absorbed bar follows the pane that is shown ──
t('a bar whose pane is hidden is let go and the shown pane\'s found', /function _hdrBarShown\(b\)\{/.test(BR) && /var had = !!_hdrBar; _hdrDrop\(\); _hdrFind\(\);/.test(BR));
t('...looked at again after every click, not only on the interval', /document\.addEventListener\('click', function\(\)\{ clearTimeout\(_hdrCT\); _hdrCT = setTimeout\(_hdrCheck, 220\); \}, true\);/.test(BR));
t('no bar left: the harness is told the bar is empty, and a shown child\'s bar is relayed instead', /groups: \[\] \}, '\*'\); \}catch\(e\)\{\} _hdrKid = null; if\(typeof _hdrRelay === 'function'\) _hdrRelay\(\);/.test(BR));
t('the harness drops an empty offer rather than holding a stale bar', /if\(!Array\.isArray\(d\.groups\) \|\| !d\.groups\.length\)\{ if\(_hdrOffers\[pid\]\)\{ if\(_hdrOffers\[pid\]\.absorbed\)\{ try\{ e\.source\.postMessage\(\{ type: 'vera:hdr:absorbed', on: false \}, '\*'\); \}catch\(_\)\{\} \} delete _hdrOffers\[pid\]; _hdrSync\(\); \} return; \}/.test(HAR)   /* 2026-09-29: and the panel is told it is no longer held */);

// ── 3. what does not fit the header is still one press away ──
t('groups that do not fit go into a harness-drawn ⋯ when the panel has no ⋯ of its own',
  /function _hdrOverOpen\(host, btn\)\{/.test(HAR) && /host\.querySelector\(':scope > \.hp-grp\[data-grp="more"\]'\)\) return;/.test(HAR) && /w\.className = 'hp-grp hp-ovw'/.test(HAR));

// ── 4. open beside, from every menu ──
// the way down to a nested entry, run for real
const wt = LHM.match(/function _wayTo\(tabs, i\)\{[\s\S]*?\n  \}/);
t('_wayTo is there', !!wt);
if (wt) {
  const _wayTo = new Function(wt[0] + '; return _wayTo;')();
  const tabs = [{ id: 'overview' }, { id: 'observe' }, { id: 's:events', depth: 1 }, { id: 's:perf', depth: 1 }, { id: 'c:scan', depth: 2 }, { id: 'c:logs', depth: 2 }, { id: 'ops' }];
  t('a top-level entry is its own id', _wayTo(tabs, 0) === 'overview' && _wayTo(tabs, 6) === 'ops');
  t('a page sub-section carries the item that shows it', _wayTo(tabs, 3) === 'observe>s:perf', _wayTo(tabs, 3));
  t('a framed child\'s section carries the item and the sub-section', _wayTo(tabs, 5) === 'observe>s:perf>c:logs', _wayTo(tabs, 5));
  const flat = [{ id: 'system' }, { id: 'calendar' }, { id: 'c:layers', depth: 1 }, { id: 'c:events', depth: 1 }, { id: 'email' }];
  t('a child section under a plain item', _wayTo(flat, 3) === 'calendar>c:events', _wayTo(flat, 3));
}
t('a docked menu\'s entry opens beside along that way', /opts\.onSplit\(cur\.id \+ '\/' \+ way\)/.test(LHM) && /if\(opts\.onSplit\) _beside\(e, function\(\)\{ opts\.onSplit\(cur\.id \+ '\/' \+ way\); \}\);/.test(LHM));
t('the bridge walks the way a step at a time, each once its level is there', /steps\.slice\(1\)\.every\(function\(x\)\{ return \/\^\[cs\]:\/\.test\(x\); \}\)/.test(BR) && /function _navChain\(steps, tok, n\)\{/.test(BR) && /if\(n < 60\) _navChain\(steps, tok, n \+ 1\);/.test(BR));
t('a nested pick asked for before its level loaded is tried again; a newer pick cancels it', /_navPend\+\+;/.test(BR) && /if\(!steps\.length \|\| tok !== _navPend\) return;/.test(BR));
t('the harness nav_select walks the way (each step is still one _navSelect)', /nav_select: _navPick,/.test(BR) && /var r = _navSelect\(\{id: steps\[0\]\}\);/.test(BR));
t('a child section is sent only to a shown child that has it', /the shown nested panel has no /.test(BR));
t('the rail\'s icons say Ctrl/middle-click opens beside', /ico\.title = m\.label \+ \(opts\.onSplit \?/.test(LHM));
t('the strips (tabs mode) open beside by Ctrl/middle-click', /if\(_splitKey\(ev\) && cfg\.onBeside\) cfg\.onBeside\(t\); else if\(cfg\.onTab\) cfg\.onTab\(t\);/.test(LHM) && /onBeside: _localLhm\[pid\] \? null : \(tab\) => \{ _lhmOpenBeside\(/.test(HAR));
t('the tab bar\'s hover list: Ctrl/middle-click and a ⧉ on every row', /bs\.className = 'tdd-bs'/.test(HAR) && /if\(beside && \(ev\.ctrlKey \|\| ev\.metaKey\)\)\{ beside\(\); return; \}/.test(HAR));
t('a panel\'s own menu in its frame asks the harness to open it beside (vera:lhm:tab)',
  /nav\.addEventListener\('click', besideAsk, true\);/.test(VP) && /nav\.addEventListener\('auxclick', besideAsk, true\);/.test(VP) && /type: 'vera:lhm:tab', id: pid\.replace\(\/--\\d\+\$\/, ''\), section: sec, by: 'you'/.test(VP));
t('...only a panel the harness framed directly, one that knows its id', /window\.parent === window\.top/.test(VP) && /if \(!inHarness \|\| !pid\) return;/.test(VP));
t('a sub-section under the lit item opens beside with its way down', /return \(a \? idOf\(a\) \+ '>' : ''\) \+ 's:' \+ sb\.getAttribute\('data-vp-sub'\);/.test(VP));
t('a new instance that its own start-up moved elsewhere is taken back to its entry (briefly)', /function _navSelectConfirm\(pid, navId, k\)\{/.test(HAR) && /_lhmNavSelect\(pid, navId\); _navSelectConfirm\(pid, navId, 0\); return;/.test(HAR));
t('an instance\'s entry opens another instance of the same panel, not an instance of the instance',
  (HAR.match(/_lhmOpenBeside\(pid\.replace\(\/--\\d\+\$\/, ''\)/g) || []).length >= 3);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
