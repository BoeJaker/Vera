// 2026-09-27 (owner): "i need all ui panels top bars to absorb into the harness top bar like the chat ui does"
//   node tests/test_panel_topbar_absorb.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const BR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-panel-bridge.js'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('every embedded panel offers its bar over the chat\'s protocol', /type: 'vera:hdr:offer', title: document\.title \|\| '', groups: groups/.test(BR));
t('except the chat, which speaks for its own', /document\.documentElement\.hasAttribute\('data-harness'\)\) return;/.test(BR));
t('a panel can name its bar, or keep it', /document\.querySelector\('\[data-vera-topbar\]'\)/.test(BR) && /=== 'keep' \? null : named/.test(BR));
t('a usual name counts only when it is the page\'s top bar', /if\(r\.width < 1 \|\| r\.top > 90 \|\| r\.height < 18 \|\| r\.height > 96 \|\| r\.width < W \* 0\.4\) continue;/.test(BR));
t('buttons, selects, fields and toggles go up', ["kind: 'select'", "kind: 'input'", "kind: 'btn', label: (_hdrText(lb)", "kind: 'btn', label: _hdrLabel(el, t)"].every((s) => BR.includes(s)));
t('the bar folds away while the harness holds it', /html\.vpb-hdr-absorbed \[data-vpb-hdr-bar\]\{display:none!important\}/.test(BR) && /classList\.toggle\('vpb-hdr-absorbed', !!d\.on\)/.test(BR));
t('a press on a proxy is a press on the panel\'s control', /d\.type !== 'vera:hdr:act' \|\| !_hdrBar/.test(BR) && /else el\.click\(\);/.test(BR));
t('a bar in a tab not yet shown is looked for again', /setInterval\(_hdrCheck, 2500\);/.test(BR) && /if\(_hdrBar && _hdrBarShown\(_hdrBar\)\)\{ _hdrOffer\(\); return; \}/.test(BR));
t('the harness draws a field proxy, keeping the caret through a re-offer', /\} else if\(it\.kind === 'input'\)\{/.test(HAR) && /el\.value = \(_foc && _foc\.hid === it\.hid\) \? _foc\.v : \(it\.value \|\| ''\);/.test(HAR) && /if\(_foc\)\{ const n = host\.querySelector/.test(HAR));
t('Enter in a field proxy is Enter in the panel', /_hdrAct\(pid, it\.hid, \{ value: el\.value, enter: true \}\)/.test(HAR) && /v\.enter\) \['keydown', 'keypress', 'keyup'\]/.test(BR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
