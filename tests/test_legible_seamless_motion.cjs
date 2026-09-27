// 2026-09-27 (owner): text size and contrast · a seamless load · the composer's controls · motion you can set · a smoother fold
//   node tests/test_legible_seamless_motion.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const UI = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-ui.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── text size: run the rewrite over a stand-in declaration ──────────────────────────────────────────────────────
{
  const a = UI.indexOf('  var TEXT_KEY = '), b = UI.indexOf('  function _walkRules(');
  t('the text engine is in vera-ui', a > 0 && b > a);
  const decl = (fs0) => { const d = { fontSize: fs0, pri: '', getPropertyPriority() { return this.pri; }, setProperty(k, v) { this.fontSize = v; } }; return d; };
  const ctx = { WeakMap, Math, parseFloat, localStorage: { getItem() { return null; } } };
  vm.createContext(ctx); vm.runInContext(UI.slice(a, b) + '\nthis.adj=_fsAdjust; this.set=function(n){ _textStep=TEXT_STEPS[n]; _textName=n; };', ctx);
  const d1 = decl('8px'), d2 = decl('9.5px'), d3 = decl('14px'), d4 = decl('1em');
  ctx.set('default'); [d1, d2, d3, d4].forEach(ctx.adj);
  t('Default raises small text to the floor', d1.fontSize === '10px' && d2.fontSize === '10px', d1.fontSize + ' ' + d2.fontSize);
  t('and leaves body text and relative sizes alone', d3.fontSize === '14px' && d4.fontSize === '1em');
  ctx.set('larger'); [d1, d2, d3].forEach(ctx.adj);
  t('Larger raises it further', d1.fontSize === '12px' && d2.fontSize === '12px', d1.fontSize);
  ctx.set('compact'); [d1, d2].forEach(ctx.adj);
  t('Compact puts back exactly what was there', d1.fontSize === '8px' && d2.fontSize === '9.5px', d1.fontSize + ' ' + d2.fontSize);
}
t('it reaches shadow roots (the widgets) as they are made', /Element\.prototype\.attachShadow = function\(\)/.test(UI));
t('and new markup as it arrives', /function _textQueue\(nodes\)/.test(UI) && /_textMO\.observe\(document\.documentElement, \{ childList:true, subtree:true \}\)/.test(UI));
t('contrast re-derives the faint tones from the theme\'s text', /html\[data-contrast="clear"\] body\{--dim:color-mix\(in srgb,var\(--text/.test(UI));
t('text and contrast follow the parent frame', /'data-text', 'data-contrast'\]\}\);/.test(UI) && /type:'vera:text'/.test(UI));
t('the controls are exported and used by the harness and the chat', /makeTextControl: _makeTextControl/.test(UI) && /veraUI\.makeTextControl\(\)/.test(HAR) && /veraUI\.makeTextControl\(\)/.test(CHAT));
t('the rows are named for what they do', /row\('Detail', \[\['full'/.test(HAR) && /lbl\.textContent = 'Zoom'/.test(HAR) && /lbl\.textContent = 'Zoom';/.test(UI));
t('the chat menu\'s small text is legible', /#rightRail\.lhm-host \.lhm-quick \.lrow \.nm small\{font-size:10px;color:var\(--t2/.test(CHAT) && /'\.lhm-side \.lhm-s-grp\{font-family:var\(--mono\);font-size:9\.5px;/.test(LHM));

// ── seamless load ───────────────────────────────────────────────────────────────────────────────────────────────
t('the page is hidden until assembled, with a failsafe', /de\.classList\.add\('vboot'\); setTimeout\(function\(\)\{ de\.classList\.remove\('vboot'\); \}, 9000\);/.test(CHAT) && /html\.vboot body\{opacity:0\}/.test(CHAT));
t('and shown once the layout is assembled', /_initRailUX\(\);\n[\s\S]{0,300}document\.documentElement\.classList\.remove\('vboot'\)/.test(CHAT));
t('the harness\'s chat starts absorbed, so its bar never flashes', /if\(q\.get\('harness'\)==='yes'&&o==='chat'\) de\.classList\.add\('hdr-absorbed'\);/.test(CHAT));
t('and a harness that never answers gives it back', /setTimeout\(\(\)=>\{ if\(!_hdrAck\) document\.documentElement\.classList\.remove\('hdr-absorbed'\); \}, 6000\);/.test(CHAT) && /_hdrAck=true;/.test(CHAT));

// ── the composer ────────────────────────────────────────────────────────────────────────────────────────────────
t('notices dock under the composer, centred on it', (CHAT.match(/\(document\.getElementById\('chatStack'\)\|\|document\.getElementById\('centre'\)\)\?\.appendChild\(dock\);/g) || []).length === 2);
t('the context strip opens from the Context chip', /function _cmpCtxMount\(\)/.test(CHAT) && /pop\.appendChild\(strip\); bar\.appendChild\(pop\);/.test(CHAT) && /const pop=document\.getElementById\('cmpCtxPop'\), chip=document\.getElementById\('cmpChipCtx'\);/.test(CHAT));
t('the chip is not given a second count (it rewrites its own)', !/className='ccn'/.test(CHAT));

// ── motion ──────────────────────────────────────────────────────────────────────────────────────────────────────
t('Motion is a Settings section', /\{id:'motion', n:'Motion', from:\[\['rpCfg','Motion'\]\]/.test(CHAT) && /<div class="sec" style="margin-top:5px">Motion<\/div>/.test(CHAT));
t('cursor styles', ['bar', 'underscore', 'dot', 'glow', 'none'].every((k) => CHAT.includes('html[data-cursor="' + k + '"] .cur')));
t('messages and capabilities arrive', /html\[data-reply="rise"\] #msgs > \.mwrap\{animation:replyRise/.test(CHAT) && /html\[data-capanim="slide"\] #msgs \.msg-body:not\(:has\(\.cur\)\) \.cap-inline/.test(CHAT));
t('a running capability shows it', /html\[data-caprun="shimmer"\] #msgs \.cap-inline:not\(\.done\):not\(\.error\)::after/.test(CHAT));
t('the stored choices are painted before the page draws', /de\.setAttribute\('data-cursor', mo\.cursor\|\|'bar'\)/.test(CHAT));

// ── a smoother fold ─────────────────────────────────────────────────────────────────────────────────────────────
t('the fold applies in place (no redraw)', /toggle: \(\) => \{ _lhmRailed = !_lhmRailed; try\{ localStorage\.setItem\('vera:lhm:rail', _lhmRailed \? '1' : '0'\); \}catch\(_\)\{\} _lhmRailApply\(\); \}/.test(HAR));
t('labels fade as the width closes', /'\.lhm-side \.lhm-s-row > :not\(\.ico\)\{transition:opacity \.22s ease\}'/.test(LHM));
t('auto-hide eases open over the page without moving it', /\.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide\{transition:width \.34s/.test(HAR) && /\.body-wrap\.lhm \.main\.ah-rail\{padding-left:54px!important\}/.test(HAR));
t('blocks off: the whole menu is see-through at any width, auto-hide reveal included', /html\[data-blocks="off"\] #lhmNav\.lhm-nav,html\[data-blocks="off"\] body\.chat-open #lhmNav\.chatmenu,html\[data-blocks="off"\] \.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide\.show\{background:transparent!important;-webkit-backdrop-filter:none!important;backdrop-filter:none!important/.test(HAR) && /html:not\(\[data-blocks="off"\]\) \.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide\.show\{/.test(HAR));
t('the chat menu\'s content fades as it folds', /#rightRail\.lhm-host\.slim > \.lhm-det > \*\{opacity:0;transition:opacity \.1s ease\}/.test(CHAT));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
