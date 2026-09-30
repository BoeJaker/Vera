// 2026-09-29 (owner): "the chat ui top bar is not absorbing into the harness top bar reliably"
// Reproduced on a cold load: the chat's own bar folded (html.hdr-absorbed) while the harness held no offer from it - the
// controls were in neither bar. Three gaps, each able to cause it and none self-healing:
//   1. an offer the harness dropped (its frame not yet among the panels') was never sent again - same signature;
//   2. the harness deleted an offer without telling the panel, which kept its bar folded;
//   3. the fold set in <head> was only undone by a fallback inside init - if init stopped first, never.
//   node tests/test_topbar_absorb_handshake.cjs      (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const CHAT = R('vera/chat/chat_panel.html'), H = R('vera/capability_orchestration.html'), BR = R('vera/chat/vera-panel-bridge.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('1. the chat re-sends its offer until the harness has answered', /setInterval\(\(\)=>_hdrOffer\(!_hdrAck\), 4000\);/.test(CHAT) && /if\(d\.type==='vera:hdr:absorbed'\)\{ _hdrAck=true;/.test(CHAT));
t('1. so does every other panel (the bridge)', /var _hdrHeard = false;/.test(BR) && /_hdrOffer\(!_hdrHeard\); return; \}/.test(BR) && /if\(d\.type === 'vera:hdr:absorbed'\)\{ _hdrHeard = true;/.test(BR));
t('2. a withdrawn offer tells the panel it is no longer held', /if\(_hdrOffers\[pid\]\.absorbed\)\{ try\{ e\.source\.postMessage\(\{ type: 'vera:hdr:absorbed', on: false \}, '\*'\); \}catch\(_\)\{\} \} delete _hdrOffers\[pid\];/.test(H));
t('2. so does an offer whose frame is no longer among the panels', /if\(!_panelIdOfSource\(o\.src\)\)\{ if\(o\.absorbed\)\{ try\{ o\.src\.postMessage\(\{ type: 'vera:hdr:absorbed', on: false \}, '\*'\); \}catch\(_\)\{\} \} delete _hdrOffers\[k\]; \}/.test(H));
t('3. the fold made in <head> is given back there if the harness never answers', /if\(q\.get\('harness'\)==='yes'&&o==='chat'\)\{ de\.classList\.add\('hdr-absorbed'\);[^\n]*if\(!_hdrHeard0\) de\.classList\.remove\('hdr-absorbed'\); \}, 9000\); \}/.test(CHAT));
t('3. and it only counts an answer from the harness itself', /if\(d&&d\.type==='vera:hdr:absorbed'&&e\.source===window\.parent\) _hdrHeard0=true;/.test(CHAT));
/* 4. THE ONE REPRODUCED EVERY TIME: a chat that loaded on a hidden tab measured its bar 0 wide and folded three groups into
   its ⋯ sheet; they came back only when the bar measured room, which a bar the harness holds never does - the harness
   showed 7 of 10 groups and the rest were in neither bar. */
const fold = CHAT.slice(CHAT.indexOf('function _chromeFold(bar, sheet){'), CHAT.indexOf('function _chromeFoldMount('));
t('4. a bar with no width (a hidden tab) is not folded', /\n    if\(!bar\.clientWidth\) return;\n/.test(fold) && fold.indexOf('if(!bar.clientWidth) return;') < fold.indexOf('for(const [k,sel,label] of _FOLD_ORDER)'));
t('4. a bar the harness holds gets every group back (the harness decides what fits)', /if\(document\.documentElement\.classList\.contains\('hdr-absorbed'\)\)\{ while\(_folded\.length\) unfold\(_folded\[_folded\.length-1\]\); document\.body\.classList\.remove\('hdr-folded'\); return; \}/.test(fold));
// the head script is one line: a // comment there would swallow the rest of it
const head = (CHAT.split('\n').find((l) => l.includes("if(q.get('harness')==='yes'&&o==='chat'){")) || '');
t('the <head> script stays one line with block comments only', head.length > 0 && !/[^:]\/\/ /.test(head.replace(/https?:\/\//g, '')));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
