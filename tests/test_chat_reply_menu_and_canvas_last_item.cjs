// 2026-10-03: the reply's action row (under the text, grouped, one "Send to" menu) and the canvas's last item
//   node tests/test_chat_reply_menu_and_canvas_last_item.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

// ── the row ──
const b = H.indexOf("actions.className='msg-actions';"), row = H.slice(b, b + 4200);
t('the row: copy, explode, canvas, then one Send to menu', /_ma\('copy','Copy'/.test(row) && /_ma\('explode','Explode'/.test(row) && /_ma\('canvas','Canvas'/.test(row) && row.includes("CH._maMenu(this)"));
t('everything that sends the message elsewhere is in the menu', ['_msgHtmlReport', '_msgPodcast', '_msgSaveToNotebook', '_msgSaveToIde', '_msgSaveToProject', '_msgThinkLater', '_msgRedeliver']
  .every((f) => new RegExp("_mi\\('[a-z]+','[^']+',`CH\\." + f).test(row)));
t('no plain button wall and no hidden-overflow trick any more', !row.includes('mact-more') && !H.includes('#msgs .msg-actions .mact:nth-child(n+6){display:none}'));
t('the menu walks with the keyboard and closes on Escape / outside', H.includes("if(e.key==='Escape'){ e.preventDefault(); const b=m.parentElement.querySelector('.ma-ddb'); _maClose();") && H.includes("if(e.key==='ArrowDown'||e.key==='ArrowUp')"));
t('handlers exposed', H.includes('_msgRedeliver,_redeliverDo,_maMenu,_maClose,_msgToCanvas,'));

// ── hover tier: under the text, never over it ──
t('hover tier no longer lays the row over the reply', !H.includes('html[data-den="hover"] .msg-actions{position:absolute') && !H.includes('html[data-den="hover"] .mwrap:hover{padding-bottom:34px}'));
t('hover tier keeps the row in its own place under the text', H.includes('html[data-den="hover"] #msgs .msg-actions{position:relative;left:auto;right:auto;bottom:auto;'));
t('an open menu rises above the pinned question', H.includes('#msgs .mwrap:has(.ma-dd.open){position:relative;z-index:80}'));

// ── the canvas's last item ──
t('the final landing waits for a live landing still in flight', /async function _cvLandReply\(mid\)\{\s*if\(_cvLiveRun\)\{ try\{ await _cvLiveRun; \}catch\(_\)\{\} \}/.test(H) && H.includes('_cvLiveRun=Promise.resolve(_cvLandFrom('));
t('a key that was already there is updated, not just shown', H.includes("if(r&&r.ok&&r.resolved==='shown'){ try{ await _capCall('canvas.update',{session_id:SID, key, content:m.content}); }catch(_){} }"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
