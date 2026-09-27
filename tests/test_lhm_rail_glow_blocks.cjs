// 2026-09-27 (owner): "its not leaving a rail when the lhm hides/collapses?" · "the glow effect seems to have gone ... the chat
// ui has an extra flat background above the glow background" · "the lhm still has its background when blocks mode is off"
//   node tests/test_lhm_rail_glow_blocks.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const CHAT = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const HAR = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const i = (s, a) => s.indexOf(a);

// the rail
t('the chat\'s menu frame keeps its rail when folded, over the hosted-menu rule that took it to nothing',
  /html\[data-only="menu"\] body #rightRail\.lhm-host\.slim\{width:100% !important;min-width:0 !important\}/.test(CHAT)
  && i(CHAT, 'html[data-only="menu"] body #rightRail.lhm-host.slim') > i(CHAT, 'html.vpb-nav-hosted #rightRail.lhm-host.slim{width:0'));
t('auto-hide leaves the rail rather than sliding the menu away', /\.body-wrap\.lhm #lhmNav\.lhm-nav\.autohide:not\(\.show\)\{transform:none;opacity:1;pointer-events:auto;width:54px!important/.test(HAR));
t('the page keeps the rail\'s width', /classList\.toggle\('ah-rail', !!\(_autohide && _lhmMode\)\)/.test(HAR) && /\.body-wrap\.lhm \.main\.ah-rail:not\(\.autohide-reserve-v\)\{padding-left:54px\}/.test(HAR));
t('and the list draws as the rail while hidden', /side\.classList\.toggle\('railed', !!_lhmRailed \|\| hidden\)/.test(HAR));

// the glow
t('the chat hides its own glow only once the harness has taken it over', /html\[data-harness\]\.hdr-absorbed #veraWash,html\[data-harness\]\[data-only="menu"\] #veraWash\{display:none\}/.test(CHAT) && !/html\[data-harness\] #veraWash\{display:none\}/.test(CHAT));
t('the chat\'s panel wrappers are see-through over the harness\'s glow', /body\.chat-open \.panels \.panel\.active,body\.chat-open \.panels \.panel\.active > div\{background:transparent!important\}/.test(HAR));
t('the chat is known open by its frame, not only once it has reported in', /iframe\[src\*="chat_panel"\]/.test(HAR));

// blocks off
t('blocks off: the chat\'s menu has no ground and no glass, in the harness frame too', /html\[data-blocks="off"\] #rightRail\.lhm-host \.lhm-det,html\[data-blocks="off"\]\[data-harness\]\[data-only="menu"\] #rightRail\.lhm-host \.lhm-det\{background:transparent;-webkit-backdrop-filter:none;backdrop-filter:none\}/.test(CHAT));
t('the translucent blocks-off ground is gone', !/html\[data-blocks="off"\] #rightRail\.lhm-host \.lhm-det\{background:color-mix/.test(CHAT));
t('blocks off: the harness menu too, unless it is floating over the page', /html\[data-blocks="off"\] #lhmNav\.lhm-nav:not\(\.show\)/.test(HAR));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
