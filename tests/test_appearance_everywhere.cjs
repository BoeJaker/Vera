// 2026-10-04 (owner): "the blocks off makes the last user message in chat have a background - this is the opposite of
// what blocks off should do. turning blocks off should not remove backgrounds from settings menus like Aa or the chat
// elipsis menu in the top bar or menus that appear over the top of other elements. could you make the 'blocks off' system
// work across the ui - all panels - and 'full hover and zen'. and the appearance options are available in the panels if
// opened standalone. can you make sure the lhm can do 'blocks off' in all panels. everything should use the same glow
// background like the chat UI."
//   node tests/test_appearance_everywhere.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (...p) => fs.readFileSync(path.join(__dirname, '..', ...p), 'utf8');
const UI = R('vera', 'vera-ui.js'), CSS = R('vera', 'ui', 'design.css'), CHAT = R('vera', 'chat', 'chat_panel.html'), TD = R('vera', 'theme_defs.py');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the surfaces paint whatever the theme names: the derived grounds fall back to the older names, and a theme saved one
// level down ({theme:{...}}) is read from inside - the active theme was, and every --surf ground painted nothing
t('the derived surfaces fall back to the older names', /"--surf":\s+"color-mix\(in srgb,var\(--ac,var\(--acc,#5a9e8f\)\) 4%,var\(--s1,var\(--bg1,#151719\)\)\)"/.test(TD) && !/var\(--ac\) \d+%/.test(TD));
t('applyVars reads a theme saved one level down', /if\(!Object\.keys\(vars\)\.some\(function\(k\)\{ return k\.indexOf\('--'\) === 0; \}\)\)\{/.test(UI) && /if\(inner\.length === 1\) vars = inner\[0\];/.test(UI));
for (const f of [['vera', 'chat', 'chat_panels_capabilities.py'], ['vera', 'ui builder', 'ui_capabilities.py']]) {
  const P = R(...f);
  t(f[f.length - 1] + ': a custom theme is flattened when made and when loaded', /var_dict = flat_theme_vars\(var_dict\)/.test(P) && /_ct\["vars"\] = flat_theme_vars\(_ct\.get\("vars"\)\)/.test(P));
}

// the chat: the pinned question has no ground with blocks off
t('chat: the pinned question has no ground and no glass with blocks off', /html\[data-blocks="off"\] body\[data-view="minimal"\]\.has-msgs #msgs > \.mwrap\.u\.pin\{background:transparent;background-image:none;border-left-color:transparent;backdrop-filter:none;-webkit-backdrop-filter:none\}/.test(CHAT)
  && !/html\[data-blocks="off"\][^{]*\.mwrap\.u\.pin\{[^}]*blur\(/.test(CHAT));
t('chat: it does not pin (so nothing scrolls behind it)', /#msgs \.mwrap\.u\.pin\{position:static\}/.test(CHAT));

// what floats over the page keeps its ground
t('design: sheets, overlays, pickers and anything marked floating keep their ground', /\[class\*="sheet"\], \[id\*="sheet"\], \[class\*="overlay"\]/.test(CSS) && /\[popover\], \[aria-modal="true"\], \[role="listbox"\], \[role="tooltip"\], \[data-vera-float\]/.test(CSS));
t('vera-ui: what is drawn fixed/absolute over the page, above it, is marked data-vera-float', /function _isFloat\(el\)\{/.test(UI) && /e\.setAttribute\('data-vera-float', ''\)/.test(UI) && /if\(c\.position === 'absolute' && r\.height >= H \* \.9 && r\.width < W \* \.9\) return false;/.test(UI));
t('vera-ui: the marks follow what changes (one observer, debounced), not a loop', /new MutationObserver\(function\(recs\)\{/.test(UI) && /t = setTimeout\(flush, 120\)/.test(UI) && !/setInterval\(_markFloats/.test(UI));

// a ground no name gives away
t('vera-ui: a large opaque in-flow part is marked data-vera-ground, measured with blocks off', /function _isGround\(el\)\{/.test(UI) && /r\.width \* r\.height < W \* H \* \.05/.test(UI) && /getAttribute\('data-blocks'\) !== 'off'\) return;/.test(UI));
t('vera-ui: turning blocks off measures again', /if\(!a\.blocks && document\.getElementById\('vera-design-css'\) && document\.body\)\{ setTimeout\(function\(\)\{ try\{ _markFloats\(document\.body\); \}catch\(e\)\{\} \}, 60\); \}/.test(UI));
t('design: a marked ground goes with blocks off; a float never', /html:root\[data-blocks="off"\] \[data-vera-ground\]:not\(\[data-vera-float\]\) \{ background-color: transparent !important;/.test(CSS));

// every panel's menu does blocks off
t('design: the shared .vp-menu and any [data-vera-lhm] menu are see-through with blocks off', /html:root\[data-blocks="off"\] :is\(\.vp-menu, nav\[data-vera-lhm\], \[data-vera-lhm\]:not\(#sidebar\)\) \{ background: transparent !important;/.test(CSS)
  && /html:root\[data-blocks="off"\] #sidebar\[data-vera-lhm\] \{ background: transparent;/.test(CSS));

// Full / Hover / Zen on every panel
t('design: Hover and Zen hold a bar\'s buttons and a row\'s actions until pointed at', /html:root:is\(\[data-den="hover"\], \[data-den="zen"\]\) body :is\(\.topbar, #topbar, header, \.toolbar/.test(CSS) && /:is\(:hover, :focus-within\)/.test(CSS));
t('design: a selected tab and anything in an open menu always show', /:not\(:is\(\.active, \.on, \[aria-selected="true"\], \[aria-pressed="true"\], \[role="tab"\], \.tab\)\)/.test(CSS) && /:is\(\[data-vera-float\], \[role="menu"\], \[class\*="menu"\], \[class\*="pop"\], \[class\*="dropdown"\]\) :is\(button, \.btn, a\.btn\) \{\s*opacity: 1;/.test(CSS));
t('design: Zen also drops the hints', /html:root\[data-den="zen"\] :is\(\.hint, \.help, \.help-text, \.opt-desc, \.field-hint\) \{ display: none; \}/.test(CSS));

// the glow on every panel
t('design: the chat\'s glow on every panel - on the body\'s own background, never under it', /html:root body \{\s*background-image: radial-gradient\(120% 100% at 10% 0%, color-mix\(in srgb, var\(--acc, #5a9e8f\) 12%, transparent\), transparent 62%\)/.test(CSS)
  && /background-attachment: fixed; background-size: 100% 360px;/.test(CSS) && !/body::before\s*\{/.test(CSS));
t('vera-ui: the content itself (paper, preview, canvas) is never a page ground', /if\(el\.matches\('\[class\*="paper"\], \[class\*="preview"\], \[class\*="receipt"\], \[class\*="canvas"\]/.test(UI));
// the pages that never loaded the shared appearance now do
for (const [name, file] of [['Gallery', ['vera', 'render', 'render_capabilities.py']], ['Sprite companion', ['vera', 'spritegen', 'spritegen_capabilities.py']], ['Thermal printer', ['vera', 'business', 'print_composer.html']]])
  t(name + ': loads /ui/vera-ui.js (appearance, glow, blocks, detail)', /<script src="\/ui\/vera-ui\.js"><\/script>/.test(R(...file)));

// standalone: the full appearance menu
t('standalone: the floating button opens the same appearance menu (style, detail, blocks, zoom, text, themes)', /btn\.onclick = function\(e\)\{\s*e\.stopPropagation\(\);\s*menu\.style\.display = 'none';\s*openAppearance\(\{ currentTarget: btn, stopPropagation: function\(\)\{\} \}\);/.test(UI)
  && /rows\.appendChild\(row\('Detail', \[\['full', 'Full'/.test(UI) && /rows\.appendChild\(row\('Blocks', \[\['on', 'On'\], \['off', 'Off'\]\]/.test(UI));
t('standalone: a low button opens the menu upwards', /menu\.style\.bottom = Math\.round\(window\.innerHeight - r\.top \+ 5\) \+ 'px';/.test(UI));

// 2026-10-04 (owner): "the details panel that opens from the right is having its background stripped if blocks are off but its
// a menu that opens over other elements - this was fixed and has regressed"; "the lhm isnt transparent on all panels like the chat
// ui lhm - so the glow background can be seen through it"
t('a fixed full-height drawer floats (keeps its ground); only in-flow parts can be a page ground', /if\(c\.position === 'fixed' \|\| c\.position === 'absolute'\) return false;/.test(UI) && !/c\.position === 'fixed' && !\(r\.height >= H \* \.9/.test(UI));
const HN = R('vera', 'capability_orchestration.html');
t('every panel\'s menu is the chat\'s glass over the glow (LHM layout)', /body:has\(\.body-wrap\.lhm\) #veraWashH\{opacity:1\}/.test(HN) && /\.body-wrap\.lhm\{position:relative;z-index:1;background:transparent\}/.test(HN)
  && /\.body-wrap\.lhm #lhmNav\.lhm-nav\{background:color-mix\(in srgb,var\(--s1,var\(--bg1,#151719\)\) 52%,transparent\);-webkit-backdrop-filter:blur\(16px\) saturate\(1\.15\)/.test(HN) && /\.body-wrap\.lhm #lhmNav \.lhm-rail\{background:transparent\}/.test(HN));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
