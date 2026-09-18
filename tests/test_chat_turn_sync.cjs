// The turn in view gets its own context, so the graph matches the message the relation edges point at
// (Notes/42 defect 99; vera/chat/chat_panel.html).
//
// Sessions do not persist the context assembled for a turn: the stored record carries only
// {role, agent_id, thinking, agent_name, latency_ms}, and the retrieved block is injected into the SYSTEM
// prompt, never into the stored question - measured on a real session, five stored questions, zero carrying it.
// So a reloaded transcript has no frames, every turn shows the same live set, and the edges point somewhere new
// on every scroll while the graph never changes. The turn's context is therefore DERIVED from its own question,
// once, through the SAME retrieval the composer uses, handed back in a SINK so CTX_NODES/CTX_EDGES - what the
// NEXT question carries - are never written.
//   node tests/test_chat_turn_sync.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the sink: ctxFetch hands the result back instead of writing the live set ──
t('ctxFetch takes a sink and knows it is in sink mode', src.indexOf("async function ctxFetch(force=false,queryOverride='',fast=false,sink=null){") >= 0 && src.indexOf('const solo=!!sink;') >= 0);
t('a derived fetch does not cancel the live one', src.indexOf('if(!solo&&_ctxAbort){_ctxAbort.abort();_ctxAbort=null;}') >= 0 && src.indexOf('if(!solo)_ctxAbort=abort;') >= 0);
t('and does not bump the version the live one checks itself against', src.indexOf('const myVersion=solo?_ctxVersion:++_ctxVersion;') >= 0);
t('stale, for a derived fetch, means only that IT was aborted', src.indexOf('const stale=()=>solo?abort.signal.aborted:(myVersion!==_ctxVersion||abort.signal.aborted);') >= 0);
t('it paints nothing on the way', src.indexOf('if(solo)return;   // a derived fetch commits once') >= 0);
t('it commits once, into the sink, touching nothing live', src.indexOf('if(solo){ sink.nodes=nodes; sink.edges=edges; sink.query=query; return; }') >= 0);
t('and never searches the live web — a scroll must not fire a search per turn', src.indexOf('const webK=solo?0:') >= 0 && src.indexOf('const newsK=solo?0:') >= 0);
t('the web and news sources are gated on those being > 0', /CTX_SRCS\.has\('web'\)[^\n]*webK>0/.test(src) && /CTX_SRCS\.has\('news'\)[^\n]*newsK>0/.test(src));

// the derivation itself must never write what the next prompt carries
const D = src.slice(src.indexOf('function _ctxDeriveFrame(w){'), src.indexOf('  // the frame in view right now'));
t('the derivation never assigns CTX_NODES/CTX_EDGES', !/CTX_NODES\s*=|CTX_EDGES\s*=/.test(D));
t('it derives at most one turn at a time and never twice for the same turn', D.indexOf('if(typeof ctxFetch!==\'function\'||_turnCtxBusy) return;') >= 0 && D.indexOf('if(_turnCtxDone[p.mid]||_frameForMid(p.mid)||_frameForMid(p.amid)) return;') >= 0);
t('a turn is a PAIR — the frame binds the question AND the answer', D.indexOf('mid:p.mid, amid:p.amid') >= 0 && src.indexOf('CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)') >= 0);
t('the follow asks for one when the turn in view has no frame', src.indexOf('if(active==null){ try{ _ctxDeriveFrame(_focusedTurn()); }catch(_){} }') >= 0);
t('ids stay numeric — updateFrameUI interpolates them unquoted into onclick', D.indexOf('id:(--_rfSeq)') >= 0 && src.indexOf('onclick="CH.loadFrame(${f.id})"') >= 0);
t('the block that never gets written is no longer parsed', src.indexOf('_ctxNodesFromPrompt') < 0 && src.indexOf('_RCMARK') < 0);

// ── _turnPair: which question a focused message belongs to ──
const mkWrap = (mid, role) => ({ dataset: { mid }, classList: { contains: (c) => c === role }, querySelector: () => ({ textContent: 'question ' + mid }) });
const wraps = [mkWrap('m1', 'u'), mkWrap('m2', 'a'), mkWrap('m3', 'u'), mkWrap('m4', 'a'), mkWrap('m5', 'u')];
const doc = { getElementById: (id) => (id === 'msgs' ? { querySelectorAll: () => wraps } : null) };
const pair = new Function('document', src.slice(884871, 885472).replace(/^\s*function /, 'function ') + '\nreturn _turnPair;')(doc);
t('a question maps to its own turn, with its answer', JSON.stringify(pair(wraps[0])) === JSON.stringify({ mid: 'm1', amid: 'm2', text: 'question m1' }), JSON.stringify(pair(wraps[0])));
t('an ANSWER maps to the same turn — the context belongs to both halves', JSON.stringify(pair(wraps[1])) === JSON.stringify({ mid: 'm1', amid: 'm2', text: 'question m1' }), JSON.stringify(pair(wraps[1])));
t('a later turn resolves to its own question, not the first', pair(wraps[3]).mid === 'm3' && pair(wraps[3]).amid === 'm4');
t('a trailing question with no answer still resolves, with no amid', pair(wraps[4]).mid === 'm5' && pair(wraps[4]).amid === '');
t('an unknown wrap yields nothing', pair(mkWrap('zz', 'u')) === null && pair(null) === null);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
