// 2026-09-27 (owner): "can any option in any of the lhms be opened in the side-by-side mode like the top level panels can so
// you could have 2 estate panels side-by side or opened by the chat panel bridge"
//   node tests/test_lhm_side_by_side.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const DIR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'ui', 'directives.py'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('Ctrl/⌘/middle-click is side by side', /function _splitKey\(ev\)\{ return !!\(ev && \(ev\.ctrlKey \|\| ev\.metaKey \|\| ev\.button === 1\)\); \}/.test(LHM));
t('a docked menu\'s items and icons pass it', /if\(_splitKey\(ev\) && opts\.onSplit\) opts\.onSplit\(cur\.id \+ '\/' \+ way\); else pickFn\(cur\.id \+ '\/' \+ t\.id\);/.test(LHM) && /if\(_splitKey\(ev\) && opts\.onSplit\) opts\.onSplit\(m\.id\); else pickFn\(m\.id\);/.test(LHM));
t('the side list\'s options and panel rows too, each with a ⧉', /if\(_splitKey\(ev\) && cfg\.onBeside\) cfg\.onBeside\(p\.id, sec, t\);/.test(LHM) && /if\(cfg\.onBeside\) _beside\(r, function\(\)\{ cfg\.onBeside\(p\.id\); \}/.test(LHM) && /if\(opts\.onSplit\) _beside\(e,/.test(LHM));
t('the harness opens it beside, a second instance when open', /function _lhmOpenBeside\(pid, navId\)\{/.test(HAR) && /if\(_openTabs\.has\(pid\)\)\{ target = _tabClone\(pid\); if\(!target\) return null; \}/.test(HAR));
t('an instance is its own tab and frame, told its id', /const cid = pid \+ '--' \+ n;/.test(HAR) && /type: 'vera:panel:init', panel_id: cid/.test(HAR) && /t2\.dataset\.cloneOf = pid;/.test(HAR));
t('at the section asked for, once the panel has published its menu', /function _navSelectWhenReady\(pid, navId\)\{/.test(HAR) && /_panelNavCache\[pid\]\)\{ _lhmNavSelect\(pid, navId\); _navSelectConfirm\(pid, navId, 0\); return; \}/.test(HAR));
t('an instance goes when it is closed', /function _tabClonesPrune\(\)\{/.test(HAR) && /try\{ _tabClonesPrune\(\); \}catch/.test(HAR));
t('menus wire it: the side list and a docked menu', /onBeside: \(pid, sec, tab\) => \{ _lhmOpenBeside\(pid\.replace\(\/--\\d\+\$\/, ''\),/.test(HAR) && /onSplit: _localLhm\[pid\] \? null : \(id => _lhmOpenBeside\(/.test(HAR));
t('the chat\'s bridge: panel.open at harness goes to the harness, with a section', /type:'vera:lhm:tab', id:String\(a\.id\|\|''\), section:sec, by:who/.test(CHAT) && /d\.type !== 'vera:lhm:tab'/.test(HAR));
t('and a section for the panel beside the chat', /action:'nav_select', action_id:'sec-'\+Date\.now\(\), payload:\{ id:sec \}/.test(CHAT));
t('the aide is told', /"args": "\{id, at, section\}"/.test(DIR) && /at=harness opens it side by side in the harness/.test(DIR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
