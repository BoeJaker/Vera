// THE GOAL: asking the next question before the last answer has finished is ordinary use, and reading back over
// the conversation with a question half-typed is too. Neither may cost a turn its context.
// (vera/chat/chat_panel.html)
//
// TWO RULES, both stated by the user:
//   "I often type the next message before the first one is complete - I want the context to change to match the
//    typed message, but I also want to be able to scroll back and still see the history with a message typed out;
//    only if I scroll right back to the bottom should the chat input box context come back."
//
// 1. OVERLAPPING TURNS. The capture was a single slot (_ctxTurnSend), so sending a second question while the
//    first was still streaming overwrote the first turn's capture and that turn got no frame at all - it then
//    fell back to the live set while scrolling, the exact complaint this feature exists to fix. Measured in a
//    browser: three questions asked without waiting produced ONE frame and two unframed turns; the same three
//    asked in sequence produced three. After: turn 2 sent while turn 1 was still generating, and both turns got
//    their own distinct frame, each bound to its own question and answer.
//
// 2. THE COMPOSER WINS ONLY AT THE BOTTOM. The rule was "is this the newest frame, and is there text in the
//    composer, within 90 seconds" - so a half-typed question blanked the newest turn's context wherever you were
//    reading, including part-way up a long final answer, and it expired on a stopwatch rather than on where you
//    were. It is positional now. Measured at six steps with three turns of known context:
//      bottom, nothing typed        -> turn 3's own context
//      bottom, question typed       -> the live set (what that question is assembling)
//      scrolled to turn 1, typed    -> turn 1's own context      <- the rule that was missing
//      scrolled to turn 2, typed    -> turn 2's own context
//      back at bottom, still typed  -> the live set again
//      bottom, composer cleared     -> turn 3's own context
//
//   node tests/test_chat_ctx_typing_and_overlap.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ---- 1. overlapping turns each keep their own capture ------------------------------------------------------
t('the capture is keyed by the question, not a single slot',
  src.indexOf('let _ctxTurnSends=new Map()') >= 0 && src.indexOf('_ctxTurnSends.set(uMsg.mid, {') >= 0);
t('the single slot is gone entirely', !/_ctxTurnSend\b/.test(src),
  'one slot means the second send overwrites the first turn in flight');
t('the turn still captures what it actually sent, at send',
  src.indexOf('nodes:JSON.parse(JSON.stringify(CTX_NODES)), edges:JSON.parse(JSON.stringify(CTX_EDGES)),') >= 0);
t('the save looks the capture up by THIS turn\'s own question',
  src.indexOf('const _ts=(_qmid&&_ctxTurnSends.get(_qmid))||{};') >= 0,
  'taking "whatever was captured last" stamps a newer question\'s context onto an older turn');
t('the question mid comes from the turn\'s own wrap, with its own uMsg as fallback',
  src.indexOf("let _qmid=(_qw0&&_qw0.dataset.mid)||'';") >= 0 &&
  src.indexOf('try{ if(!_qmid && uMsg && uMsg.mid) _qmid=uMsg.mid; }catch(_){}') >= 0);
t('only this turn is retired from the map; others in flight keep theirs',
  src.indexOf('if(_qmid) _ctxTurnSends.delete(_qmid);') >= 0);

// a late context fetch says which records the prompt carried - but only the turn it belongs to, and with more
// than one in flight there is no way to tell which question it was for
t('the late-fetch patch is attributable or it does not happen',
  src.indexOf('function _ctxPatchSentIds(nodes){') >= 0 &&
  src.indexOf('if(_ctxTurnSends.size!==1 || !_ctxTurnSendLast) return;') >= 0);
t('and both fetch paths go through it', (src.match(/_ctxPatchSentIds\(nodes\);/g) || []).length === 2,
  'call sites only - the declaration is not one');

// ---- 2. the composer's context belongs to the bottom -------------------------------------------------------
t('"at the bottom" is a real measurement of the transcript',
  src.indexOf('return m.scrollTop+m.clientHeight>=m.scrollHeight-8;') >= 0);
t('the composer wins only at the bottom, with something typed',
  src.indexOf('function _ctxComposerLive(){') >= 0 &&
  src.indexOf('if(!_ctxAtBottom()) return false;') >= 0 &&
  src.indexOf('return !!(inp&&inp.value.trim());') >= 0);
t('a turn pinned by clicking it is an explicit choice and is not overridden',
  src.indexOf('if(_grFocusMid) return false;') >= 0);
t('the frame lookup uses that rule',
  src.indexOf('if(_ctxComposerLive() && f===CTX_FRAMES[CTX_FRAMES.length-1]) return null;') >= 0);
t('and the stopwatch is gone - where you are reading decides, not how long ago you typed',
  src.indexOf('Date.now()-_ctxTypingT<90000') < 0);

// ---- the safety property from the same feature still holds --------------------------------------------------
t('scrolling still cannot rewrite what the next question carries',
  src.indexOf('return { nodes:CTX_NODES, edges:CTX_EDGES, frame:null };') >= 0 &&
  (src.match(/CTX_NODES=JSON\.parse/g) || []).length === 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
