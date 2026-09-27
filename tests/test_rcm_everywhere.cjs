// 2026-09-27 (owner): "the RCM of the design bleeding edge also needs extending to other UIs ... and the existing thermal
// print option integrated into the new RCM"
//   node tests/test_rcm_everywhere.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const RCM = R('vera/ui/rcm.js'), MEN = R('vera/ui/menus.js'), HAR = R('vera/capability_orchestration.html'), BR = R('vera/chat/vera-panel-bridge.js'), CHAT = R('vera/chat/chat_panel.html'), PS = R('vera/business/vera-print-selection.js'), LIBS = R('vera/ui/libs.py');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

{ const ctx = { window: {} }; vm.createContext(ctx); vm.runInContext(MEN, ctx); const M = ctx.window.MENUS;
  const tx = M.rows('text', 'hello'); t('a selection is a kind: copy, print, ask', ['copy', 'print', 'ask'].every((id) => tx.some((r) => r.id === id)));
  t('every kind ends with thermal print', M.rows('host', 'ct126').some((r) => r.id === 'print') && M.rows('widget', 'x').some((r) => r.id === 'print') && M.rows('nothing-known', 'x').some((r) => r.id === 'print')); }
t('the runtime: targets, the menu, the runner, thermal print', /window\.VeraRCM = \{/.test(RCM) && /fetch\('\/print\/text'/.test(RCM) && /function runner\(cap, arg, tgt, cx, cy\)/.test(RCM));
t('it stands aside for a page with its own menu, and for Shift', /if \(ev\.shiftKey \|\| window\.__veraRcmOwn \|\| !window\.MENUS\) return;/.test(RCM) && /window\.__veraRcmOwn=true;/.test(CHAT));
t('fields and canvases keep their own menus', /\.cmenu,\.crun,input,textarea,select,\[contenteditable="true"\],canvas,video/.test(RCM));
t('served', /@APP\.get\("\/ui\/rcm\.js"/.test(LIBS));
t('the harness loads it and binds its tabs, menu rows and tiles', /<script src="\/ui\/menus\.js"><\/script>\n<script src="\/ui\/rcm\.js"><\/script>/.test(HAR) && /function _rcmAttach\(\)\{/.test(HAR) && /q\('#dashGrid > \.widget'\)/.test(HAR));
t('every panel loads it through the bridge', /r\.src = '\/ui\/rcm\.js';/.test(BR) && /m\.src = '\/ui\/menus\.js';/.test(BR));
t('the chat\'s own menu prints and knows a selection', /case 'print': \{ const t=String\(x\.text/.test(CHAT) && /return \['text', sel\.slice\(0,40\)/.test(CHAT));
t('the floating print button steps aside where the menu is', /if\(window\.VeraRCM \|\| window\.__veraRcmOwn\)\{ hide\(\); return; \}/.test(PS));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
