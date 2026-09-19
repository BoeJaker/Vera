// THE GOAL: open a chat, talk for many turns, scroll back, and each turn shows the context that was ACTUALLY
// USED for it. (vera/chat/chat_panel.html)
//
// Showing today's context at every turn is what the graph already did, and is NOT the ask. So nothing here
// re-queries: a turn's context exists only because it was captured as that turn was sent, and that captured
// set is the only thing ever shown. A turn from before this code ran has none and is not given a fabricated
// one.
//
// Three things make it work, and each failed in a measurable way before:
//  1. captured at SEND. Reading live CTX_NODES when the reply landed read the NEXT question's context, or
//     nothing at all.
//  2. EVERY completed turn gets a frame, even an empty one. A turn without one falls back to the live set and
//     shows the newest turn's context - exactly the "current context at every turn" complaint. Measured
//     before: only one turn in three ended up with a frame.
//  3. the frame is bound to its question and answer FROM THE DOM, and read back off the focused message.
//     Every identifier tried before drifted: HISTORY counts gave two frames both labelled "Turn 2", and mids
//     are reissued on every render.
//   node tests/test_chat_turn_context.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

t('the turn carries the context it actually sent, captured at send',
  src.indexOf('nodes:JSON.parse(JSON.stringify(_allN)), edges:JSON.parse(JSON.stringify(_allE)),') >= 0);
t('every completed turn gets a frame, even an empty one',
  src.indexOf('const _f=_saveFrame(') >= 0 && src.indexOf('if(_sn){ _saveFrame(') < 0 && src.indexOf('CTX_NODES.length?CTX_NODES.slice():[]') >= 0);
t('the frame is tagged onto its answer and its question, from the DOM',
  src.indexOf('_aw.dataset.frame=String(_f.id);') >= 0 && src.indexOf("_qw0.classList.contains('u')") >= 0);
t('the lookup reads that tag off the focused message',
  src.indexOf('const fid=w&&w.dataset.frame;') >= 0);
t('with mid/amid only as a fallback for frames that have no wrap to tag',
  src.indexOf('f=CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)||null;') >= 0);
t('_saveFrame stores the snapshot handed to it, not whatever is live',
  src.indexOf('const _src=Array.isArray(x.nodes)?x.nodes:CTX_NODES') >= 0);
t('a very long chat cannot grow without bound',
  src.indexOf('if(CTX_FRAMES.length>500)CTX_FRAMES.splice(0,CTX_FRAMES.length-500);') >= 0);
// ...but only AT THE BOTTOM. Scrolled up, the message in view keeps its own context even with a question
// half-typed, so you can read back over the conversation without losing what you were writing; the typed
// question's context returns when you return to the end. See test_chat_ctx_typing_and_overlap.cjs.
t('the newest turn still yields to the live set while the next question is typed',
  src.indexOf('if(_ctxComposerLive() && f===CTX_FRAMES[CTX_FRAMES.length-1]) return null;') >= 0);

// nothing may re-query a past turn: that yields TODAY's context, which is the wrong answer
for (const dead of ['_ctxComputeOlder', '_turnOf', '_oldCtxBusy', '_oldCtxDone'])
  t('no re-querying a past turn: ' + dead + ' is absent', src.indexOf(dead) < 0);
t('ctxFetch is untouched - the sink had only that one caller',
  src.indexOf("async function ctxFetch(force=false,queryOverride='',fast=false){") >= 0 && src.indexOf('solo=!!sink') < 0);
// and nothing persists: surviving a restart is explicitly not the goal
for (const dead of ['_CTXK_SCOPE', '_ctxTurnPersist', '_ctxTurnsRestore'])
  t('no server persistence: ' + dead + ' is absent', src.indexOf(dead) < 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
