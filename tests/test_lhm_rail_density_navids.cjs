// 2026-09-27 (owner):
//   * "the lhm seems to have a bug - i cant select sub menu options for things like the estate"
//   * "can the LHM in the chat ui and the overall lhm collapse to a rail with icons"
//   * "can it also respect the full - hover - zen styles only displaying the icons inline to the menu options in full mode"
//   node tests/test_lhm_rail_density_navids.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const VP = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-panel.js'), 'utf8');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const EST = fs.readFileSync(path.join(__dirname, '..', 'vera', 'workers', 'workers_ollama_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── an id names one item: run the shell's own rule over the Estate's real buttons ─────────────────────────────
{
  const a = VP.indexOf("    var ID_ATTRS = "), b = VP.indexOf("    // title attribute first");
  t('the rule is in the shell', a > 0 && b > a);
  const btnsOf = (html) => [...html.matchAll(/<button[^>]*class="nav-btn[^"]*"[^>]*>/g)].map((m) => { const at = {}; for (const x of m[0].matchAll(/(data-[\w-]+)="([^"]*)"/g)) at[x[1]] = x[2]; return { getAttribute: (k) => (k in at ? at[k] : null) }; });
  const run = (btns) => { const ctx = { btns }; vm.createContext(ctx); vm.runInContext(VP.slice(a, b) + '\nthis.ids = btns.map(idOf);', ctx); return ctx.ids; };
  const est = btnsOf(EST).filter((x) => x.getAttribute('data-view') === 'estate');
  const ids = run(est);
  t('the Estate publishes one id per item', ids.length >= 15 && new Set(ids).size === ids.length, JSON.stringify(ids.slice(0, 6)));
  t('Live ops is its own item', ids.includes('ops') && ids.includes('estate') && ids.includes('overview'), JSON.stringify(ids));
  // a panel whose ids were already unique keeps exactly the ids it had
  const plain = [{ a: { 'data-view': 'one', 'data-tab': 'x1' } }, { a: { 'data-view': 'two', 'data-tab': 'x2' } }].map((o) => ({ getAttribute: (k) => o.a[k] || null }));
  t('a panel with unique ids keeps them', JSON.stringify(run(plain)) === '["one","two"]', JSON.stringify(run(plain)));
  const none = [{ getAttribute: (k) => (k === 'data-view' ? 'v' : null) }, { getAttribute: (k) => (k === 'data-view' ? 'v' : null) }];
  t('and with nothing unique it falls back to the old reading', JSON.stringify(run(none)) === '["v","v"]');
}

// ── the rail ───────────────────────────────────────────────────────────────────────────────────────────────────
t('the side menu has a fold control and a railed state', /if\(cfg\.rail\)\{ var rl = _el\('button', 'lhm-s-rl'/.test(LHM) && /if\(cfg\.rail && cfg\.rail\.on\) wrap\.classList\.add\('railed'\);/.test(LHM));
t('railed, a panel is its icon and the open one is lit', /\.lhm-side\.railed \.lhm-s-row > :not\(\.ico\)\{opacity:0;pointer-events:none\}/.test(LHM) && /\.lhm-side\.railed \.lhm-s-row\.on\{box-shadow:inset 2px 0 0 var\(--acc\)\}/.test(LHM));
t('and every panel row names itself on hover', /r\.setAttribute\('data-w', 'panel · ' \+ \(p\.label \|\| p\.id\)\); r\.title = p\.label \|\| p\.id;/.test(LHM));
t('the harness keeps the fold, remembered', /localStorage\.getItem\('vera:lhm:rail'\) === '1'/.test(HAR) && /rail: \{ on: _lhmRailed, toggle:/.test(HAR) && /#lhmNav\.lhm-nav\.railed:not\(\.chatmenu\)\{width:54px!important/.test(HAR));
t('the chat\'s menu tells the harness when it folds, and the slot folds with it', /if\(_EMBED\.only==='menu'\) window\.parent\.postMessage\(\{ type:'vera:lhm:rail', on:r\.classList\.contains\('slim'\) \}, '\*'\);/.test(CHAT) && /d\.type !== 'vera:lhm:rail'\) return; _lhmChatRailed = !!d\.on;/.test(HAR) && /#lhmNav\.lhm-nav\.absorbed\.chatmenu\.railed\{width:47px!important/.test(HAR));

// ── icons by tier ─────────────────────────────────────────────────────────────────────────────────────────────
t('Zen leaves the words', /html\[data-den="zen"\] \.lhm-side:not\(\.railed\) \.lhm-s-row \.ico,html\[data-den="zen"\] \.lhm-top \.lhm-row \.lhm-ri\{display:none\}/.test(LHM));
t('Hover shows an icon when its option is pointed at', /html\[data-den="hover"\] \.lhm-side:not\(\.railed\) \.lhm-s-row:hover \.ico/.test(LHM));
t('the chat\'s own option lists follow the tier, closing the icon column in Zen', /html\[data-den="zen"\] #rightRail\.lhm-host \.lhm-quick \.tm-r\{grid-template-columns:1fr auto\}/.test(CHAT));
t('a folded rail is its icons in every tier', /\.lhm-side\.railed \.lhm-s-row \.ico\{display:inline-flex!important;opacity:1!important\}/.test(LHM));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
