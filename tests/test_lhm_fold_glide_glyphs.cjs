// 2026-09-28 (owner):
//   * "the lhm could do with better animations between its various modes - particularly going from glyph rail to lhm,
//      the glyphs need to line up with their menu options properly"
//   * "the menu option and their glyph in the rail (or letter) should be properly vertically aligned with each other"
//   * "the glyphs on the top level menu need to be bigger"
//   * "the configurable text sizes need to be larger - step them up a notch or two"
//   node tests/test_lhm_fold_glide_glyphs.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const LHM = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'vera-lhm.js'), 'utf8');
const UI = fs.readFileSync(path.join(__dirname, '..', 'vera', 'vera-ui.js'), 'utf8');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// ── a word given as an icon is its first letter; a symbol stays as given ─────────────────────────────────────────
{
  const a = LHM.indexOf('  function _glyph('), b = LHM.indexOf('  // the fold, animated (FLIP)');
  t('the glyph rule is in the library', a > 0 && b > a);
  const ctx = { window: { Intl }, Intl, Symbol, Array, String };
  vm.createContext(ctx); vm.runInContext(LHM.slice(a, b) + '\nthis.g=_glyph;', ctx);
  t('a word is its first letter', ctx.g('pokedex', '▭') === 'P', ctx.g('pokedex', '▭'));
  t('a symbol is kept', ctx.g('⚙', '▭') === '⚙' && ctx.g('▷_', '▭') === '▷_');
  t('nothing given is the fallback', ctx.g('', '▭') === '▭' && ctx.g(null, '•') === '•');
  t('an emoji with its variation selector is kept whole', ctx.g('⚙️', '▭') === '⚙️');
}

// ── every menu glyph goes through the rule: the side list, the top-level list, both rails ────────────────────────
t('the side list rows', /_el\('span', 'ico', _glyph\(o\.icon, '▭'\)\)/.test(LHM) && /_el\('span', 'ico', _glyph\(p\.icon, '▭'\)\)/.test(LHM));
t('the top-level list rows', (LHM.match(/_el\('span', 'lhm-ri', _glyph\(/g) || []).length === 3);
t('the quick menu rail and an absorbed rail', /m\.iconHtml \? null : _glyph\(m\.icon, '•'\)/.test(LHM) && /\(m\.id === act\.menu \? ' on' : ''\), _glyph\(m\.icon, '•'\)\)/.test(LHM));

// ── a glyph sits in a fixed, centred box, larger than before ─────────────────────────────────────────────────────
t('the side list glyph: a 22 px box, 16 px, centred', /'\.lhm-side \.lhm-s-row \.ico\{width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;line-height:1;[^']*font-size:16px/.test(LHM));
t('the top-level list glyph: a 22 px box, 16 px, centred', /'\.lhm-top \.lhm-row \.lhm-ri\{width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;line-height:1;[^']*font-size:16px/.test(LHM));
t('the rail glyphs are larger', /'\.lhm-rail \.lhm-ico\{[^']*font-size:18px/.test(LHM) && /'\.lhm-rail \.lhm-ico\.top\{font-size:18px/.test(LHM) && /#rightRail\.lhm-host \.lhm-rail \.lhm-ico\{[^}]*font-size:18px\}/.test(CHAT) && /#rightRail\.lhm-host \.lhm-rail \.lhm-ico svg\{width:20px;height:20px\}/.test(CHAT));
t('folded, the box keeps its centring', /'\.lhm-side\.railed \.lhm-s-row \.ico\{display:inline-flex!important;opacity:1!important\}'/.test(LHM));

// ── the fold glides: whoever folds the side menu, its rows travel between their two places ───────────────────────
t('side() watches its own fold', /if\(cfg\.rail && cfg\.rail\.on\) wrap\.classList\.add\('railed'\);\n    _watchFold\(wrap\);/.test(LHM));
t('the watch reads the class the menu left', /attributeFilter: \['class'\], attributeOldValue: true/.test(LHM));
t('a class set as the list is drawn is not a fold', /Date\.now\(\) - born < 250/.test(LHM));
t('FLIP: measured in both states, our own toggles discarded', /wrap\.classList\.toggle\('railed', wasRailed\); var first = at\(\); wrap\.classList\.toggle\('railed', nowRailed\);/.test(LHM) && /_lhmFlipMO\.takeRecords\(\)/.test(LHM));
t('the glide keeps the width\'s easing', /duration: 340, easing: 'cubic-bezier\(\.2,\.8,\.2,1\)'/.test(LHM));
t('what the rail hides fades back in', /@keyframes lhmUnfold/.test(LHM) && /\.lhm-side\.lhm-unfold \.lhm-s-grp/.test(LHM));
t('reduced motion is respected', /prefers-reduced-motion: reduce\)'\)\.matches\) return;/.test(LHM) && /@media \(prefers-reduced-motion:reduce\)\{\.lhm-side\.lhm-unfold \*/.test(LHM));

// ── the fold, run: a stand-in menu whose rows move 40 px between the two states ──────────────────────────────────
{
  const a = LHM.indexOf('  function _railFlip('), b = LHM.indexOf('  // watch a side menu for the fold');
  const anims = [];
  const mkEl = (railY, fullY) => ({ getBoundingClientRect() { return { left: 10, top: wrap.railed ? railY : fullY, height: 30 }; }, animate(k, o) { anims.push({ k, o }); } });
  const wrap = { railed: false, isConnected: true, offsetWidth: 1, animate() {}, _cls: new Set(),
    classList: { contains(c) { return c === 'railed' ? wrap.railed : wrap._cls.has(c); }, toggle(c, on) { if (c === 'railed') wrap.railed = on; }, add(c) { wrap._cls.add(c); }, remove(c) { wrap._cls.delete(c); } },
    querySelectorAll() { return rows; } };
  const rows = [mkEl(80, 120), mkEl(110, 150)];
  const ctx = { window: {}, matchMedia: () => ({ matches: false }), setTimeout: () => 0, clearTimeout() {}, Math, Array };
  vm.createContext(ctx); vm.runInContext(LHM.slice(a, b) + '\nthis.flip=_railFlip;', ctx);
  ctx.flip(wrap, true);   // unfolded: was the rail, now the list
  t('each row glides from its rail place to its list place', anims.length === 2 && anims[0].k[0].transform === 'translate(0px,-40px)' && anims[0].k[1].transform === 'none', JSON.stringify(anims[0]));
  t('unfolding fades the hidden parts in', wrap._cls.has('lhm-unfold'));
  anims.length = 0; ctx.flip(wrap, false);
  t('no fold, no motion', anims.length === 0);
}

// ── the text sizes, stepped up ───────────────────────────────────────────────────────────────────────────────────
t('every step raised, and a Largest past the old top', /'default':\{ floor:11, factor:1\.08 \}, large:\{ floor:12, factor:1\.2 \}, larger:\{ floor:13, factor:1\.32 \}, largest:\{ floor:14, factor:1\.45 \}/.test(UI) && /\['largest','Largest','The largest'\]/.test(UI));

if (fails) { console.log(fails + ' FAILED'); process.exit(1); } else console.log('all ok');
