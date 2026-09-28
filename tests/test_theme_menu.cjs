// The shell's Aa menu: anchored to the control that was pressed (the old #themeBtn is hidden in the header sheet, so the
// menu opened off-screen), and it carries the style pack and the density tier - full, hover, zen - beside the themes.
//   node tests/test_theme_menu.cjs
const fs = require('fs'), path = require('path');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'capability_orchestration.html'), 'utf8');
const U = fs.readFileSync(path.join(R, 'vera', 'vera-ui.js'), 'utf8');
let fails = 0;
const t = (name, ok, why) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : '  ' + (why || ''))); if (!ok) fails++; };
t('the Aa control in the header opens the theme menu', /<button class="ico" type="button" onclick="openThemeMenu\(event\)"[^>]*>[^<]*<span[^>]*>Aa<\/span>/.test(H));
t('the menu is anchored to the control that was pressed, with the hidden #themeBtn only as the fallback', H.includes("const btn = (ev && ev.currentTarget && ev.currentTarget.getBoundingClientRect && ev.currentTarget.offsetParent) ? ev.currentTarget : document.getElementById('themeBtn');"));
t('the menu carries the style pack (standard · news · term · pixel), the density tier (full · hover · zen) and the blocks switch, through vera-ui', H.includes('function _appearanceRowsEl(){') && H.includes("menu.appendChild(_appearanceRowsEl());") && H.includes("[['standard', 'Standard'], ['newspaper', 'News'], ['terminal', 'Term'], ['pixel', 'Pixel']]") && H.includes("[['full', 'Full', 'Every control shown'], ['hover', 'Hover', 'Controls appear on hover'], ['zen', 'Zen', 'The page alone']]") && H.includes('vu.setDensity(v)') && H.includes('vu.setStyle(v)') && H.includes('vu.setBlocks('));
t('vera-ui owns those settings and broadcasts them', U.includes("setDensity: function(id){ return setAppearance({den:id}).den; }") && U.includes("setStyle: function(id){ return setAppearance({style:id}).style; }") && U.includes("setBlocks: function(on){ return setAppearance({blocks:on}).blocks; }"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
