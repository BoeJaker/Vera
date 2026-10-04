// The chat's automatic context fetches (typing, after a reply, the pre-send guard) search memory without the vector
// leg, which waits for an embedding and holds every later source behind it (5.31 s vs 0.38 s, identical top 12,
// 2026-10-04); only a fetch the user asks for (force) waits for it.
//   node tests/test_chat_ctx_preview_backends.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const H = fs.readFileSync(path.join(__dirname, '..', 'vera/chat/chat_panel.html'), 'utf8');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
const i = H.indexOf('async function ctxFetch(force=false,queryOverride=\'\',fast=false){');
const body = i >= 0 ? H.slice(i, i + 6000) : '';
t('ctxFetch keeps its (force, queryOverride, fast) signature', i >= 0);
t('automatic fetches search postgres+neo4j only; a forced, non-fast fetch includes the vector leg',
  body.includes("if(fast||!force) mBody.backends='postgres,neo4j';"));
t('the old unconditional "fast only" rule is gone', !body.includes("if(fast) mBody.backends='postgres,neo4j';"));
t('the user\'s own fetches still pass force=true', H.includes('onclick="CH.ctxFetch(true)"'));
t('the typing preview does not force', H.includes('CTX_TIMER=setTimeout(()=>ctxFetch(false,v),600);'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
