// 2026-10-03 (owner), the LHM audit:
//   6  "sub LHMs should not use letters as icons in the rail"
//   10 "menu options have descriptions and those descriptions are visible on mouseover and could even aide the agents to
//       operate the panel bridge or for agents to navigate the pages" - "possibly a short and long, short mainly for ui use
//       the long for agentic usage"
//   1  (decision) "one icon per group for menu items that have sub items"
//   node tests/test_lhm_desc_icons.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const BR = R('vera', 'chat', 'vera-panel-bridge.js'), LHM = R('vera', 'chat', 'vera-lhm.js'), VP = R('vera', 'vera-panel.js'), CHAT = R('vera', 'chat', 'chat_panel.html');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const LETTERS = /^[A-Za-z0-9]{1,2}$/;

// ── the bridge: what a panel's menu publishes ───────────────────────────────────────────────────────────────────────
{
  const a = BR.indexOf('  var _navLhmOpts = null;'), b = BR.indexOf('  // ── DOM helpers');
  const ctx = { document: { title: 'Vera — Estate', querySelector: () => null } }; vm.createContext(ctx);
  vm.runInContext('var _navItems = null, _navActiveId = ""; function _shownKidCta(){ return null; }  /* no nested panel shown: the nested primary action is tested in test_ui_bootstrap */\n' + BR.slice(a, b) + '\nthis.lhm=_navLhm; this.set=function(i,a){ _navItems=i; _navActiveId=a; };', ctx);
  // grouped: two named groups, the first with its own mark and descriptions on its heading
  ctx.set([
    { id: 'ov', label: 'Overview', group: 'Overview', icon: '', desc: 'The estate at a glance', desc_long: 'Every machine and service, healthy or not.', group_icon: '◫', group_desc: 'Start here', group_desc_long: 'The estate summary pages.' },
    { id: 'map', label: 'Map', group: 'Overview', icon: '⋈', desc: 'The topology', desc_long: '' },
    { id: 'docker', label: 'Docker', group: 'Machines', icon: 'D', desc: '', desc_long: '' },
  ], 'map');
  const s = ctx.lhm();
  t('one rail icon per group', s.menus.length === 2, s.menus.length);
  const g0 = s.menus[0], g1 = s.menus[1];
  t('a group carries its heading\'s own mark', g0.icon === '◫', g0.icon);
  t('a group carries its heading\'s descriptions', g0.desc === 'Start here' && g0.desc_long === 'The estate summary pages.');
  t('a group with no mark of its own offers its first item\'s for the host to fall back on', g1.icon === '' && g1.icon_alt === '', g1.icon + '|' + g1.icon_alt);
  t('an item keeps its descriptions in the list', g0.tabs[0].desc === 'The estate at a glance' && g0.tabs[0].desc_long === 'Every machine and service, healthy or not.');
  t('an item keeps its own mark', g0.tabs[1].icon === '⋈');
  t('a LETTER given as an item\'s mark is dropped, not published', g1.tabs[0].icon === '', g1.tabs[0].icon);
  t('no published icon anywhere is letters', [].concat(...s.menus.map((m) => [m.icon, m.icon_alt].concat(m.tabs.map((x) => x.icon)))).every((i) => !LETTERS.test(i || '')));
  // flat: no groups - one rail icon per item, each with its descriptions
  ctx.set([{ id: 'a', label: 'Runs', desc: 'Loop runs', desc_long: 'All runs.' }, { id: 'b', label: 'Settings' }], 'a');
  const f = ctx.lhm();
  t('flat: an icon per item, its description on it', f.menus.length === 2 && f.menus[0].desc === 'Loop runs' && f.menus[0].desc_long === 'All runs.');
  t('flat: an item with no mark publishes none (the host maps its name)', f.menus[1].icon === '' && !LETTERS.test(f.menus[0].icon));
}
t('the bridge never makes an initial', !/charAt\(0\)[^;]*toUpperCase/.test(BR.slice(BR.indexOf('function _navIcon'), BR.indexOf('function _navLhm'))));
t('registerNav keeps desc (140) and desc_long (600) and the group\'s mark and descriptions', /o\.desc = cl\(it\.desc, 140\); o\.desc_long = cl\(it\.desc_long, 600\);/.test(BR) && /o\.group_icon = cl\(it\.group_icon, 4\)/.test(BR) && /o\.group_desc_long = cl\(it\.group_desc_long, 600\)/.test(BR));
t('agents read them: the published state\'s nav.items are those items', /st\.nav = \{items: _navItems, active: _navActiveId\}/.test(BR));
t('a nested child\'s descriptions ride up with its sections', /desc: String\(it\.desc \|\| ''\)\.slice\(0, 140\), desc_long: String\(it\.desc_long \|\| ''\)\.slice\(0, 600\)/.test(BR) && /id: 'c:' \+ it\.id, label: String\(it\.label \|\| it\.id\), desc: it\.desc \|\| '', desc_long: it\.desc_long \|\| ''/.test(BR));

// ── the shell: where the descriptions come from in a panel's markup ─────────────────────────────────────────────────
t('an item: data-desc, data-desc-long', /function descOf\(b\) \{ var d = \(b\.getAttribute\('data-desc'\)/.test(VP) && /function descLongOf\(b\) \{ return \(b\.getAttribute\('data-desc-long'\)/.test(VP));
t('a group heading: data-icon, data-desc, data-desc-long', /it\.group_icon = \(ge\.getAttribute\('data-icon'\)/.test(VP) && /it\.group_desc_long = \(ge\.getAttribute\('data-desc-long'\)/.test(VP));
t('standalone, the hover shows it too - with the name pinned first', /if \(!b\.getAttribute\('data-label'\)\) b\.setAttribute\('data-label', it\.label\); b\.title = it\.label \+ ' (?:\\u2014|\u2014) ' \+ it\.desc;/.test(VP));

// ── the library: the glyph a group or item is drawn with, and its hover ─────────────────────────────────────────────
{
  const a = LHM.indexOf('  var _KW_GLYPHS = ['), b = LHM.indexOf('  // the fold, animated (FLIP)');
  const ctx = { window: {}, Array, String }; vm.createContext(ctx);
  vm.runInContext(LHM.slice(a, b) + '\nthis.mg=_menuGlyph; this.tip=_tip; this.gf=glyphFor; this.N=GLYPH_NEUTRAL;', ctx);
  t('a group\'s own mark wins', ctx.mg({ icon: '◫', label: 'Machines', icon_alt: '▤' }) === '◫');
  t('else its name\'s glyph', ctx.mg({ icon: '', label: 'Machines', icon_alt: '▤' }) === ctx.gf('Machines') && ctx.gf('Machines') !== ctx.N);
  t('else its first item\'s mark', ctx.mg({ icon: '', label: 'Zzyzx', icon_alt: '▤' }) === '▤');
  t('else the one neutral mark - never a letter', ctx.mg({ icon: 'Z', label: 'Zzyzx', icon_alt: 'Q' }) === ctx.N);
  const names = ['Overview', 'Docker', 'Email', 'Calendar', 'Settings', 'Research', 'Storage', 'Loops', 'Zzyzx', 'Pokedex', 'Q'];
  t('every name maps to a mark, none of them letters', names.every((n) => { const g = ctx.gf(n); return g && !LETTERS.test(g); }), names.map((n) => n + '=' + ctx.gf(n)).join(' '));
  t('"Docker" is a machine, not a document', ctx.gf('Docker') === ctx.gf('Machines'));
  t('the hover: "Label — short description"', ctx.tip('Docker', 'Containers on every host') === 'Docker — Containers on every host');
  t('the hover with no description is the label', ctx.tip('Docker', '') === 'Docker' && ctx.tip('Docker', 'Docker') === 'Docker');
}
t('an absorbed rail icon and list item show the short one and carry the long one', /_descOn\(ico, m\); ico\.title = _tip\(m\.label, m\.desc\)/.test(LHM) && /e\.title = _tip\(t\.label, t\.desc\); _descOn\(e, t\);/.test(LHM) && /el\.setAttribute\('data-desc-long', String\(o\.desc_long\)\)/.test(LHM));
t('the chat\'s rail, the harness\'s side list and its strips do too', /ico\.title = m\.desc \? _tip\(m\.label, m\.desc\)/.test(LHM) && /r\.title = _tip\(p\.label \|\| p\.id, p\.desc\); _descOn\(r, p\);/.test(LHM) && /h\.title = _tip\(sec\.label \|\| sec\.id, sec\.desc\)/.test(LHM) && /b\.title = _tip\(s\.label \|\| s\.id, s\.desc\)/.test(LHM));
t('the chat publishes its descriptions (spec + navState)', /desc: m\.desc \|\| '', desc_long: m\.desc_long \|\| '', badge: b/.test(LHM) && /label: t\.label, desc: t\.desc \|\| '', desc_long: t\.desc_long \|\| ''/.test(LHM) && /label: m\.label, desc: m\.desc, desc_long: m\.desc_long/.test(LHM));
t('the host can map a name to its glyph', /window\.VeraLHM = \{ glyphFor: glyphFor, glyph: _glyph,/.test(LHM));

// ── the chat, the template: every one of its menus and their tabs is described, short and long ──────────────────────
{
  const a = CHAT.indexOf('  const LHM_MENUS=['), b = CHAT.indexOf('  ];', a);
  const block = CHAT.slice(a, b);
  const menus = block.match(/\{id:'[a-z]+',\s*icon:/g) || [];
  t('the chat has its ten menus (Settings joined the eight; Canvas - the session canvas controls - 2026-10-11)', menus.length === 10, menus.length);
  t('each menu has a short and a long description', (block.match(/icon:'[^']+',\s*label:'[^']+',\s*desc:'/g) || []).length === 10 && (block.match(/\n\s*desc_long:'/g) || []).length === 10);
  const tabs = block.match(/\{id:'[A-Za-z]+',label:'[^']+'[^}]*\}/g) || [];
  t('each tab has both too', tabs.length >= 12 && tabs.every((x) => /desc:'/.test(x) && /desc_long:'/.test(x)), tabs.filter((x) => !/desc_long/.test(x)).join(' | '));
}

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
