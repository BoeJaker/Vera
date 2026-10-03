// 2026-10-03 (owner), the LHM audit, point 5: "the settings menu in the chat ui (and therefor any other that have them)
// should use the full height available to the lhm (not leaving the top portion of the lhm with the burger button visible
// - it must be covered) - that settings menu is the cannon settings menu style for all panels."
//   node tests/test_lhm_settings_covers_slot.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'capability_orchestration.html'), 'utf8');
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

// the chain: the menu instance opens its page and asks the slot for it; the harness marks the slot .page
t('the chat\'s menu instance asks for the slot when Settings opens', /if\(_EMBED\.only==='menu'\)\{ try\{ window\.parent\.postMessage\(\{ type:'vera:lhm:grow', on:_spOpen\|\|!!_ctxGrown, page:_spOpen\?'settings':''/.test(C));
t('the harness marks the slot as showing a page', /nav\.classList\.toggle\('page', !!\(d\.on && d\.page\)\)/.test(H));
t('inside its frame the page covers the whole viewport, with its own title and close', /#settingsPage\{position:fixed;top:0;right:0;bottom:0;left:0;/.test(C) && /if\(ev\.key==='Escape'&&_spOpen\)\{ _settingsPage\(false\); \}/.test(C));
// the fix: everything in the slot but the menu frame steps aside - the ☰ bar (drawn into #lhmNavTabs), the feed, the foot
t('everything in the slot but the menu frame steps aside while it shows a page', /#lhmNav\.lhm-nav\.chatmenu\.page > :not\(#lhmMenuHost\)\{display:none!important\}/.test(H));
t('and the menu frame takes the full height', /#lhmNav\.lhm-nav\.chatmenu\.page > #lhmMenuHost\.on\{flex:1 1 auto;height:100%\}/.test(H));
t('the ☰ bar is drawn into #lhmNavTabs, a sibling of the frame, so the rule reaches it', /function _lhmNavSync\(\)\{\n  const host = document\.getElementById\('lhmNavTabs'\);/.test(H) && /const bar = document\.createElement\('div'\); bar\.className = 'lhm-topbar';/.test(H) && /tabs\.parentNode\.insertBefore\(hostEl, tabs\.nextSibling\)/.test(H));
t('the slot\'s children are the tab list, the feed, the foot and the frame', /<nav class="lhm-nav" id="lhmNav">\n\s*<div id="lhmNavTabs"/.test(H) && /<div class="lhm-nav-foot">/.test(H));
// scoped to the chat's menu: with the chat closed (.chatmenu off) a stale .page hides nothing
t('scoped to the chat\'s menu, so a stale .page never blanks another panel\'s menu', /document\.getElementById\('lhmNav'\)\?\.classList\.toggle\('chatmenu', chatOpen\);/.test(H) && !/#lhmNav[^{]*(?<!chatmenu)\.page > :not/.test(H.replace(/#lhmNav\.lhm-nav\.chatmenu\.page > :not/g, '')));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
