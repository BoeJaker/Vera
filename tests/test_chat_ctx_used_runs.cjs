// WSU-62: the context -> chat runs leave out a memory the turn was given and did not use (memory.feedback's
// injected_ids / used_ids); without a verdict every lit record runs.   node tests/test_chat_ctx_used_runs.cjs
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');
const P = fs.readFileSync(path.join(__dirname, '..', 'vera', 'fabric', 'memory_hooks.py'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('the server says what the turn was given as well as what it used', P.includes('"injected_ids": [v["id"] for v in verdicts][:60]'));
t('the chat listens for its session\'s memory.feedback (one socket per page, renewed with the session)',
  H.includes("if((m.event||m.type)!=='memory.feedback'||String(m.session_id||'')!==String(SID||'')) return; _ctxUseNote(m);")
  && H.includes('if(!SID||(_ctxUseEs&&_ctxUseSid===SID)) return;'));
t('the verdict is kept for the turn just judged (frame question/answer, newest blocks, the menu page\'s focus)',
  H.includes('const fr=CTX_FRAMES[CTX_FRAMES.length-1]; if(fr){ if(fr.mid) keys.add(String(fr.mid)); if(fr.amid) keys.add(String(fr.amid)); }'));
t('a given-but-unused memory has no run; without a verdict every lit record runs',
  H.includes('all.filter(p=>p.lit&&!(_uv&&_uv.injected.has(String(p.id))&&!_uv.used.has(String(p.id)))).forEach'));
t('the harness can feed a verdict (CH._ctxUseNote)', H.includes('_persistCanvasCfg,_ctxUseNote,_cvLookStep,_cvRelevance,'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
