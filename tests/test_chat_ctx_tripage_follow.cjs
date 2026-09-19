// THE GOAL: on the board the chat and the rail's menu are TWO INSTANCES of this page, and the mini context
// graph lives in the menu. Scrolling the chat must move the menu's graph onto the turn in view, and a past turn
// must show the SAME families it showed live. (vera/chat/chat_panel.html)
//
// Both of these were reported by the user against a build that passed its own tests, because those tests
// injected .mwrap elements INTO the menu instance - something that never happens in reality.
//
// 1. A FRAME WAS MISSING FIVE FAMILIES. CTX_NODES holds what was RETRIEVED (memory, vector, graph, fabric,
//    web...). The agent, its skills, its ontologies, its capabilities and the related Q&A are synthesised at
//    render time by _ctxAssembledExtra from the LIVE _assembledCtx, and were never in CTX_NODES - so a frame
//    built from CTX_NODES alone could not contain them, and every past turn drew a visibly poorer graph than it
//    had shown live ("missing some of the sections like caps, graph, skills"). They are captured WITH the turn
//    now, while _assembledCtx still describes that question, which also makes a frame self-contained.
//    Measured after, on real turns: turn 2's frame reads
//      memory:13, cap:33, ontology:13, graph:8, fabric:5, related_qa:3, skill:4, agent:1
//    where before it read memory/graph/fabric only.
//
// 2. THE FOLLOW WAS DEAD IN THE SPLIT. Three separate breaks, all of which had to be fixed together:
//      a. the menu renders NO transcript, so _focusedTurn() found no .mwrap and _ctxActiveCompute returned null
//         on every scroll - the graph always drew the live set;
//      b. the chat instance broadcast 'focus' only from _ctxColumnSync, which runs only when the graph COLUMN is
//         mounted, so on the board it broadcast nothing at all;
//      c. #msgs' scroll listener was bound only from the rail's quick menu and from mounting that column -
//         neither of which exists in the chat instance - so the chat had NO scroll listener whatsoever.
//    Measured before: scrolling the chat through three turns left the menu's shares, budget bar and drawn
//    records byte-identical at every position. After, on real turns: chat at the bottom -> menu draws turn 2's
//    frame; chat at the top -> menu draws turn 1's, which is a genuinely different set.
//
//   node tests/test_chat_ctx_tripage_follow.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ---- 1. the frame carries the whole prompt, not just the retrieved records ---------------------------------
t('the assembled families are captured WITH the turn',
  src.indexOf('const _ex=_ctxAssembledExtra(_CTX_ALL_LAYERS, CTX_NODES);') >= 0 &&
  src.indexOf('_allN=CTX_NODES.concat(_ex.nodes.filter(n=>!CTX_NODES.some(c=>c.id===n.id)));') >= 0);
t('and the frame stores that, not CTX_NODES alone',
  src.indexOf('nodes:JSON.parse(JSON.stringify(_allN)), edges:JSON.parse(JSON.stringify(_allE)),') >= 0,
  'a frame built from CTX_NODES has no caps, skills, ontologies, agent or related Q&A');
t('they count as sent, because they were in the prompt',
  src.indexOf('ids:new Set(_allN.map(n=>n.id))') >= 0,
  'marking them as read-by-the-response would put them on the wrong side of the frame');
t('the live set still adds them at render time, so it is unchanged',
  src.indexOf('if(!_V.frame){ try{ const ex=_ctxAssembledExtra(') >= 0);

// ---- 2a. the menu has no transcript, so it is told which turn is in view ------------------------------------
t('with no transcript in this page, the turn in view is the one broadcast to it',
  src.indexOf("if(!mid && !document.querySelector('#msgs .mwrap')) mid=_grFocusMid||'';") >= 0);

// ---- 2b. the chat broadcasts the focused turn on every scroll, not only with the column open -----------------
t('the follow itself broadcasts the focused turn',
  src.indexOf("if(m!==_ctxFocusSent){ _ctxFocusSent=m; _lhmPost('focus',{ mid:m }); }") >= 0);
t('an EMPTY mid is broadcast too - it means "show the live set"',
  src.indexOf("const w=_focusedTurn(); const m=_ctxComposerLive()?'':(w?(w.dataset.mid||''):'');") >= 0,
  'the menu has neither the transcript nor the composer, so the instance with both decides');
t('and only an instance that HAS a transcript broadcasts',
  src.indexOf("if(_EMBED.only!=='menu'&&document.querySelector('#msgs .mwrap')){") >= 0);
t('the menu acts on it: recompute the frame and repaint every surface',
  src.indexOf("else if(m.act==='focus'){ _grFocusMid=a.mid||'';") >= 0 &&
  src.indexOf('try{ _ctxFollow(true); }catch(_){} }') >= 0);
t('the focus handler no longer drops an empty mid', src.indexOf("m.act==='focus'&&a.mid") < 0);

// ---- 2c. the transcript's scroll listener is bound for ANY instance that has one -----------------------------
t('the scroll listener is bound at mount, not only by the rail menu or the graph column',
  src.indexOf('try{ _ctxFollowBind(); }catch(_){}\n  if(!document.getElementById(\'msgs\')||!document.getElementById(\'msgs\')._ctxFollowBound){') >= 0,
  'the chat instance has neither of those, so #msgs carried no scroll listener at all');
t('and it is retried briefly for pages that build #msgs later',
  src.indexOf("const m=document.getElementById('msgs'); if((m&&m._ctxFollowBound)||++_fbT>40) clearInterval(_fb); }, 250);") >= 0);
t('binding stays idempotent, so the existing call sites are unaffected',
  src.indexOf('if(msgs&&!msgs._ctxFollowBound){ msgs._ctxFollowBound=true;') >= 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
