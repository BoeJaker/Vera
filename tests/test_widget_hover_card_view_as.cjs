// 2026-09-27 (owner): "even better coverage on detail when an item in a widget is hovered or clicked" · "some widgets could
// transform to others to give a different view instead of haveing tonnes of realted spread out widgets"
//   node tests/test_widget_hover_card_view_as.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const WE = fs.readFileSync(path.join(__dirname, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('every part that carries an item has a tip', WE.includes("closest('[data-b],[data-tip],[title],[data-item]')"));
t('the card: name, fields formatted, nested counted, more, where from, related', /function tipCard\(host, el\)/.test(WE) && /'<div class="vw-tkv">'/.test(WE) && /' related'/.test(WE) && /more field/.test(WE));
t('built once per part, not on every move', WE.includes("host._tipHtml = el ? tipCard(host, el) : '';") && WE.includes('e.clientX, e.clientY, act, host._tipHtml);'));
t('view as: the family\'s forms that would draw the answer', /function viewsFor\(rec, form0, data\)/.test(WE) && /if \(!isEmpty\(dataFor\(mapped\(rec, f, data\), f\)\)\) out\.push\(f\);/.test(WE));
t('the pick draws in place and is kept per widget', /localStorage\.setItem\('vera\.widget\.as\.' \+ key\(rec\), this\._viewAs\)/.test(WE) && /const form = \(this\._viewAs && viewsFor\(rec, form0, this\._data\)\.includes\(this\._viewAs\)\) \? this\._viewAs : form0;/.test(WE) && /this\._viewAsWire\(form0, form, rec\);/.test(WE));
t('not while a dashboard is arranged; the menu floats so a tile cannot clip it', /:host-context\(\.dash-grid\.editing\) \.vw-as\{display:none\}/.test(WE) && /\.vw-as-m\{position:fixed;/.test(WE));
// 2026-09-28 / 2026-10-01 (owner): "the view as menu should only appear if the switch arrows icon is pressed not on mouseover" ·
// "the ⇄ view buttons modal often displays cut off the right edge of the screen and it opens the menu if the mouse is over the widget"
t('nothing about view-as changes on the widget\'s hover: no reveal on :host(:hover), the ⇄ always faintly there', !/:host\(:hover\) \.vw-as/.test(WE) && /\.vw-as\{position:absolute;[^}]*opacity:\.38/.test(WE) && /\.vw-as:hover,\.vw-as:focus-within,\.vw-as\.open\{opacity:1\}/.test(WE));
t('the ⇄ carries no title, so the hover card never pops a card over it', !/class="vw-as-b" title=/.test(WE) && /class="vw-as-b" aria-label="View this as another form"/.test(WE));
t('the list opens only on the ⇄ press', /b\.addEventListener\('click', \(ev\) => \{ ev\.stopPropagation\(\); if \(!m\.hidden\)/.test(WE) && !/b\.addEventListener\('(pointerenter|mouseenter|mouseover|pointerover)'/.test(WE));
t('the list stays on screen: measured without the scrollbar, and shifted by where it actually landed (a containing block)', /const vw = document\.documentElement\.clientWidth \|\| window\.innerWidth/.test(WE) && /const got = m\.getBoundingClientRect\(\); if \(Math\.abs\(got\.left - want\.x\) > 1/.test(WE));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
