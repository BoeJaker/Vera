// The Forgetting controls (owner, 2026-10-05: "add controls for the user to select what gets forgotten and how quickly").
//   node tests/test_forgetting_controls.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera/chat/chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('a section of the Context pane, a row of the Settings page', H.includes('<div class="sec" style="margin-top:5px">Memory fading</div>') && H.includes("{id:'fading', n:'Forgetting', from:[['rpCtx','Memory fading']]"));
t('it reads the policy and the last dry run', H.includes("api('/memory/fade/policy','POST',{}), api('/memory/fade/report','POST',{})"));
t('per class: may fade, half-life (blank = never), floor, and what it would forget today', H.includes('class="f-on"') && H.includes('placeholder="never"') && H.includes('class="f-fl"') && H.includes("'<td class=\"rt\">'"));
t('save sends every class\'s settings; defaults and a fresh dry run are one press', H.includes("api('/memory/fade/policy','POST',{set, by:'chat settings'})") && H.includes("{reset:true, by:'chat settings'}") && H.includes("api('/memory/fade/report','POST',{refresh:true})"));
t('it says faded memories are set aside, not deleted, and come back', H.includes('Faded memories are set aside, not deleted: a strong match brings one back, and using it revives it.'));
t('opening the row loads it', H.includes("if(id==='fading'){ try{ _fadeLoad(); }catch(_){} }"));
t('colours from the theme only', /\.fade-tbl th\{[^}]*var\(--dim, #7d8590\)/.test(H) && !/\.fade-[a-z-]+\{[^}]*color:#/.test(H));
t('nightly fading is a switch, off unless ticked', H.includes('class="f-auto"') && H.includes("api('/memory/fade/policy','POST',{auto:!!ev.target.checked, by:'chat settings'})"));
t('fading now takes two presses, says how many, and never uses a prompt dialog', H.includes("fb.textContent='Press again to fade';") && H.includes("api('/memory/fade/run','POST',{confirm:true, by:'chat settings'})") && !/_fade[A-Za-z]*[^\n]{0,400}prompt\(/.test(H));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
