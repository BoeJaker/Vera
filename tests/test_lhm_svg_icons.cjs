// 2026-10-03 (owner), the LHM audit, point 6: "sub LHMs should not use letters as icons in the rail". Dream, the DAG
// workshop and Telegram draw their menu icons as SVG; the shell only read text glyphs, so the docked rail showed initials
// (and, after the no-letters change, name glyphs or the neutral mark) instead of the panels' own icons. The SVG now rides
// in the spec and the harness draws it - rebuilt by a whitelist cleaner, never inserted as given.
//   node tests/test_lhm_svg_icons.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const LHM = R('vera', 'chat', 'vera-lhm.js'), BR = R('vera', 'chat', 'vera-panel-bridge.js'), VP = R('vera', 'vera-panel.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

const a = LHM.indexOf('  var _KW_GLYPHS = ['), b = LHM.indexOf('  // the motion tokens');
const ctx = { window: {}, Array, String }; vm.createContext(ctx);
vm.runInContext(LHM.slice(a, b) + '\nthis.clean=_svgClean; this.icon=_menuIcon; this.N=GLYPH_NEUTRAL; this.gf=glyphFor;', ctx);
const C = ctx.clean;
const OVERVIEW = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/></svg>';

// ── the cleaner ────────────────────────────────────────────────────────────────────────────────────────────────────
t('a plain icon is kept, shape for shape', C(OVERVIEW) === OVERVIEW, C(OVERVIEW));
t('a <script> anywhere: no icon at all', C('<svg viewBox="0 0 1 1"><script>alert(1)</script><path d="M0 0"/></svg>') === '');
t('an event attribute: no icon at all', C('<svg viewBox="0 0 1 1" onload="alert(1)"><path d="M0 0"/></svg>') === '' && C('<svg><path d="M0 0" onclick="x()"/></svg>') === '');
t('<use>, <image>, <a>, <foreignObject>, <style>: no icon at all', ['<use href="#x"/>', '<image href="x.png"/>', '<a href="javascript:x"><path d="M0 0"/></a>', '<foreignObject><div/></foreignObject>', '<style>*{}</style>'].every((x) => C('<svg>' + x + '</svg>') === ''));
t('an attribute not on the list is dropped, the icon kept', C('<svg viewBox="0 0 1 1" style="color:red" class="x"><path d="M0 0"/></svg>') === '<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>');
t('url(), data: and script in a value: that attribute dropped', C('<svg><path d="M0 0" fill="url(#g)" stroke="javascript:x"/></svg>') === '<svg><path d="M0 0"/></svg>');
t('<title>/<desc> dropped quietly', C('<svg><title>Overview</title><path d="M0 0"/></svg>') === '<svg><path d="M0 0"/></svg>');
t('unbalanced or not an svg: nothing', C('<svg><g><path d="M0 0"/></svg>') === '' && C('<div><svg/></div>') === '' && C('hello') === '' && C(null) === '');
t('too large: nothing', C('<svg>' + '<path d="M0 0"/>'.repeat(400) + '</svg>') === '');

// ── which icon a menu gets ─────────────────────────────────────────────────────────────────────────────────────────
const I = ctx.icon;
t('the panel\'s own mark first', I({ icon: '⚙', icon_svg: OVERVIEW, label: 'Settings' }).text === '⚙');
t('then its own SVG, before any name glyph', I({ icon: '', icon_svg: OVERVIEW, label: 'Overview' }).html === OVERVIEW);
t('an SVG the cleaner refuses falls back to the name glyph', I({ icon: '', icon_svg: '<svg><script/></svg>', label: 'Overview' }).text === ctx.gf('Overview'));
t('a group: its name glyph, then its first item\'s SVG, then the neutral mark', I({ icon: '', label: 'Zzyzx', icon_alt: '', icon_alt_svg: OVERVIEW }).html === OVERVIEW && I({ icon: '', label: 'Zzyzx' }).text === ctx.N);

// ── every real menu icon of the three panels survives the cleaner ───────────────────────────────────────────────────
for (const [name, file] of [['Dream', ['vera', 'dream', 'dream_panel.html']], ['DAG workshop', ['vera', 'dag', 'dag_workshop_panel.html']], ['Telegram', ['vera', 'telegram', 'telegram_panel.html']]]) {
  const H = R(...file);
  const svgs = [...H.matchAll(/<button class="nav-btn[^>]*>\s*(<svg[\s\S]*?<\/svg>)/g)].map((m) => m[1]);
  t(name + ': all ' + svgs.length + ' menu icons are drawn', svgs.length >= 6 && svgs.every((s) => C(s) !== ''), svgs.filter((s) => C(s) === '').map((s) => s.slice(0, 60)).join(' | '));
}

// ── the pipe ───────────────────────────────────────────────────────────────────────────────────────────────────────
t('the shell hands an item\'s SVG on when it has no text mark', /if \(!it\.icon\) \{ var sv = b\.querySelector && b\.querySelector\('svg'\); if \(sv && sv\.outerHTML && sv\.outerHTML\.length <= 3000\) it\.icon_svg = sv\.outerHTML; \}/.test(VP));
t('the bridge carries it (item, flat menu, group\'s first item)', /if\(it\.icon_svg && String\(it\.icon_svg\)\.length <= 3000\) o\.icon_svg = String\(it\.icon_svg\);/.test(BR) && /icon_svg: it\.icon_svg \|\| '', label: it\.label, title: title/.test(BR) && /icon_alt_svg: g0\.icon_svg \|\| ''/.test(BR));
t('the rail draws the cleaned SVG, never the raw one', /var mi = _menuIcon\(m\), ico = _el\('div', 'lhm-ico' \+ \(m\.id === act\.menu \? ' on' : ''\), mi\.html \? null : mi\.text\); if\(mi\.html\) ico\.innerHTML = mi\.html;/.test(LHM) && !/innerHTML = m\.icon_svg/.test(LHM));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
