// An older message with no recorded context gets one computed, once, and kept
// (Notes/42 defect 106; vera/chat/chat_panel.html).
//
// Restored messages have no frame because their context was never recorded: the stored record carries
// graph_id:'' and relations:[], its metadata is only {role, agent_id, thinking, agent_name, latency_ms}, and
// the session graph's edges are message/activity links. So scrolling up showed nothing new.
// Turns sent in the page already capture and keep their own context and compute nothing. This covers the rest:
// at most once per message for the life of the page, never for one that already has a frame, never more than
// one at a time, through a SINK so the set the next question carries is never written.
//   node tests/test_chat_older_context.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

t('ctxFetch can hand its result back instead of writing the live set', src.indexOf("async function ctxFetch(force=false,queryOverride='',fast=false,sink=null){") >= 0 && src.indexOf('const solo=!!sink;') >= 0);
t('a computed turn cannot cancel or invalidate the live fetch', src.indexOf('if(!solo&&_ctxAbort){_ctxAbort.abort();_ctxAbort=null;}') >= 0 && src.indexOf('const myVersion=solo?_ctxVersion:++_ctxVersion;') >= 0);
t('it paints nothing and commits once, into the sink', src.indexOf('if(solo)return;   // a computed turn commits once') >= 0 && src.indexOf('if(solo){ sink.nodes=nodes; sink.edges=edges; sink.query=query; return; }') >= 0);
t('and never searches the live web', src.indexOf('const webK=solo?0:') >= 0 && src.indexOf('const newsK=solo?0:') >= 0);

const D = src.slice(src.indexOf('function _ctxComputeOlder(w){'), src.indexOf('  // the frame in view right now'));
t('computing an older turn never writes the live set', !/CTX_NODES\s*=|CTX_EDGES\s*=/.test(D));
t('a message that already has a frame is never recomputed', D.indexOf('if(p.q.dataset.frame||_oldCtxDone[p.mid]) return;') >= 0);
t('it is remembered even when it comes back empty, so a barren message is asked once', D.indexOf('_oldCtxDone[p.mid]=true;') >= 0 && D.indexOf('_oldCtxDone[p.mid]=true;') < D.indexOf('ctxFetch(true,'));
t('at most one at a time', D.indexOf("if(typeof ctxFetch!=='function'||_oldCtxBusy) return;") >= 0);
t('the result is bound to the question AND the answer, from the DOM', D.indexOf('p.q.dataset.frame=String(f.id); if(p.a) p.a.dataset.frame=String(f.id);') >= 0);
{ const iFree = D.indexOf("_oldCtxBusy='';"), iFollow = D.indexOf('_ctxFollow(true)');
  t('the lock is freed BEFORE the follow, so the next message can be asked for', iFree >= 0 && iFollow > iFree); }
t('only a turn with no frame triggers it', src.indexOf('if(active==null){ try{ _ctxComputeOlder(_focusedTurn()); }catch(_){} }') >= 0);

// the question/answer walk, run for real
const mk = (cls, mid, txt) => ({ classList: { contains: (c) => cls.split(' ').includes(c) }, dataset: { mid },
  previousElementSibling: null, nextElementSibling: null, querySelector: () => (txt == null ? null : { textContent: txt }) });
const q1 = mk('mwrap u', 'm1', 'first question'), a1 = mk('mwrap a', 'm2', 'reply');
q1.nextElementSibling = a1; a1.previousElementSibling = q1;
const document = { getElementById: () => ({}) };
const walk = new Function('document', src.slice(src.indexOf('  function _turnOf(w){'), src.indexOf('  function _ctxComputeOlder(w){')) + '\nreturn _turnOf;')(document);
t('a question resolves to itself and its answer', (() => { const r = walk(q1); return r && r.mid === 'm1' && r.amid === 'm2' && r.text === 'first question'; })());
t('its ANSWER resolves to the same turn', (() => { const r = walk(a1); return r && r.mid === 'm1' && r.amid === 'm2'; })());
const lone = mk('mwrap u', 'm9', 'trailing question');
t('a question with no answer yet still resolves, with no amid', (() => { const r = walk(lone); return r && r.mid === 'm9' && r.amid === ''; })());
t('nothing resolves to nothing', walk(null) === null);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
