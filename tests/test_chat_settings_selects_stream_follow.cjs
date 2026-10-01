// 2026-10-01: the settings' drop menus open over the Settings page and in the menu page; the page covers the header;
// no style selector in its head; a streaming reply follows the tail without jerking
//   node tests/test_chat_settings_selects_stream_follow.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const z = (re) => { const m = H.match(re); return m ? +m[1] : NaN; };

const zMenu = z(/\.vsel-m\{position:fixed;z-index:(\d+);/), zPage = z(/#settingsPage\{position:fixed;[^}]*z-index:(\d+);/);
t('a drop menu opens above the Settings page', zMenu > zPage);
t('the Settings page covers the whole height (no gap for the header)', /#settingsPage\{position:fixed;top:0;right:0;bottom:0;left:0;/.test(H));
t('the Settings head carries no style selector', !H.includes("w.className='sp-hd-pack'") && !H.includes('.sp-hd-pack{'));
t('the Appearance section keeps its style pack card', H.includes("c1.setAttribute('data-w','style pack · card')"));
t('the menu page keeps the drop menu it draws on <body>', /html\[data-only="menu"\] body > \.vsel-m:not\(#_\)\{display:block!important\}/.test(H)
  && H.indexOf('body > .vsel-m:not(#_)') > H.indexOf('html[data-only="menu"] body > :not(:has(#rightRail)){display:none!important}'));
t('following the tail is instant, never a smooth glide', /function _toBottom\(b\)\{ try\{ b\.scrollTo\(\{ top:b\.scrollHeight, behavior:'instant' \}\);/.test(H)
  && /function scroll\(force\)\{[\s\S]{0,200}_toBottom\(b\);[\s\S]{0,200}if\(_stickToBottom\)\{\s*_toBottom\(b\);/.test(H));
t('a streaming reply cannot shrink while it repaints', /bubEl\.style\.minHeight=h0\+'px'[\s\S]{0,200}bubEl\._hold=setTimeout\(\(\)=>\{ bubEl\.style\.minHeight=''; \}, 1200\)[\s\S]{0,80}_paintStream\(bubEl, fullText\);/.test(H));
t('the floor is the content box (no growth by the padding each frame)', H.includes("pad=cs.boxSizing==='border-box'?0:"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
