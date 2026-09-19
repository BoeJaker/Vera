// THE GOAL: scroll back through an active session and EVERY surface that shows context shows the context of the
// turn in view - not just the graph plot. (vera/chat/chat_panel.html)
//
// Measured on the design edge before this landed, at three scroll positions in one session (three turns holding
// deliberately different families):
//
//   position        plot draws   budget bar / "In this prompt" / meter   legacy SVG   legacy list
//   turn 3 (end)    turn 3       ontology,web,cap,skill,agent            54 nodes     live
//   turn 1 (top)    turn 1       ontology,web,cap,skill,agent            54 nodes     live
//   turn 2 (mid)    turn 2       ontology,web,cap,skill,agent            54 nodes     live
//
// Only the plot followed, because <vera-context-graph> does its own frame swap (context_graph_element.js: it
// draws frame.nodes when a frame is active). Every OTHER surface reached into the live globals independently,
// so the graph pointed at turn 1 while the bar, the list, the meter and both legacy surfaces described what the
// NEXT question was assembling. Five readings of one thing, disagreeing on screen.
//
// The fix is ONE accessor, not a fifth parallel path: _ctxInView(). The split it draws is the safety property -
//   DISPLAY surfaces read _ctxInView()            -> they follow the scroll
//   PROMPT-BUILDING sites read CTX_NODES/CTX_EDGES -> they never do
// Cross that line and scrolling silently rewrites the context your next message carries. The legacy Frames pane
// did exactly that: loadFrame ASSIGNED the globals.
//
//   node tests/test_chat_ctx_in_view.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// the body of a top-level function, up to the next one declared at the same indent
function body(decl) {
  const i = src.indexOf(decl);
  if (i < 0) return null;
  const j = src.indexOf('\n  function ', i + decl.length);
  return src.slice(i, j < 0 ? src.length : j);
}

// ---- the accessor itself ----------------------------------------------------------------------------------
t('_ctxInView exists', src.indexOf('function _ctxInView(){') >= 0);
t('it returns the active frame\'s own snapshot when a turn is in view',
  src.indexOf('if(f) return { nodes:(f.nodes||[]), edges:(f.edges||[]), frame:f };') >= 0);
t('and the live set otherwise - including on any error, so a display bug cannot blank the graphs',
  src.indexOf('return { nodes:CTX_NODES, edges:CTX_EDGES, frame:null };') >= 0);

// ---- every DISPLAY surface reads it, and none of them still reaches for the globals ------------------------
for (const [name, decl] of [
  ['the legacy SVG graph', 'function renderCtxGraph(){'],
  ['the legacy record list', 'function renderCtxList(){'],
  ['the fabric pane', 'function renderFabPane(){'],
]) {
  const b = body(decl);
  t(name + ' reads the turn in view', !!b && b.indexOf('_ctxInView()') >= 0);
  t(name + ' no longer reads the live globals directly', !!b && b.indexOf('CTX_NODES') < 0 && b.indexOf('CTX_EDGES') < 0,
    'a display surface reading CTX_NODES is one that cannot follow the scroll');
}
// _ctxShares drives THREE surfaces at once: the budget bar (which is also the section control), the rows under
// "In this prompt", and the header meter. It may still name CTX_NODES, but only inside the live-set branch:
// the assembled extras (agent, skills, ontologies, caps) belong to the live set alone - adding today's to a past
// turn would invent context that turn never had.
const shares = body('function _ctxShares(){');
t('the bar, the list and the meter read the turn in view', !!shares && shares.indexOf('let nodes=_V.nodes.slice();') >= 0);
t('the assembled extras are added to the LIVE set only', !!shares && shares.indexOf('if(!_V.frame){ try{ const ex=_ctxAssembledExtra(') >= 0);

// ---- the other half of the split: the prompt still carries the LIVE set -----------------------------------
t('ctxFetch still assigns the live set', src.indexOf('CTX_NODES=nodes;CTX_EDGES=edges;_ctxQuery=query;') >= 0);
t('the send path still captures the live set at send', src.indexOf('nodes:JSON.parse(JSON.stringify(_allN)), edges:JSON.parse(JSON.stringify(_allE)),') >= 0);
t('include/exclude still toggles the live set', src.indexOf('function _ctxExcludeAll(){CTX_NODES.forEach(n=>n.included=false);renderCtxList();}') >= 0);
t('_ctxInView is not used where the prompt is built', (body('function _ctxInView(){') || '') && src.slice(src.indexOf('const items=CTX_NODES'), src.indexOf('const items=CTX_NODES') + 400).indexOf('_ctxInView') < 0);

// ---- and the surfaces actually repaint when the turn in view changes ---------------------------------------
t('one repaint path, so a surface cannot be added and silently never redraw',
  src.indexOf('function _ctxViewRepaint(){') >= 0);
t('the scroll follow repaints through it', src.indexOf('_ctxActiveFrame=active; _ctxViewRepaint();') >= 0);
t('the grown column repaints through it too', src.indexOf('_ctxActiveFrame=active; _ctxViewRepaint(); try{ _ctxRunsDraw(); }catch(_){} }') >= 0);
t('the grown budget bar repaints when the turn in view changes, not only when the shares do',
  src.indexOf("+'#'+JSON.stringify(_qGalOff)+'#'+_ctxActiveFrame;") >= 0,
  'two turns can hold the same families at the same token counts');

// ---- the budget bar is the section control; the chips are gone ---------------------------------------------
t('the chips are gone', src.indexOf('class="gal-mix"') < 0 && src.indexOf(".querySelectorAll('.gm')") < 0);
t('each band of the bar is its family\'s switch',
  src.indexOf("el.querySelectorAll('.comp i[data-fam]').forEach(c=>c.addEventListener('click',()=>{ const f=c.dataset.fam; _qGalOff[f]=!_qGalOff[f]; _ctxViewRepaint(); }));") >= 0);
t('an off band keeps its place and a trace of its colour, or it could never be pressed again',
  src.indexOf('.comp i[data-fam].off{background:color-mix(in srgb,var(--c) 20%,transparent)}') >= 0 &&
  src.indexOf('.comp i[data-fam]{cursor:pointer;min-width:7px}') >= 0);

// ---- ONE source filter: the legacy layer row reads the bar's state ----------------------------------------
// The .lbtn row kept a Set of families that are ON while the bar kept _qGalOff of those that are OFF - two
// controls over the same records, so switching Memory off on the graph left it lit on the legacy list.
t('_ctxLayers is a read of _qGalOff, not a second Set',
  src.indexOf('has:(f)=>!_qGalOff[String(f)],') >= 0 &&
  src.indexOf("let _ctxLayers=new Set(['vector','graph','memory'") < 0);
t('a family with no entry is ON, which is what membership of the old Set meant',
  src.indexOf('add:(f)=>{ delete _qGalOff[String(f)]; },') >= 0 && src.indexOf('delete:(f)=>{ _qGalOff[String(f)]=true; }') >= 0);
t('the legacy layer row is redrawn from that state, not toggled in place',
  src.indexOf("document.querySelectorAll('[data-ctx-layer]').forEach(b=>b.classList.toggle('on', _ctxLayers.has(b.dataset.ctxLayer)));") >= 0,
  'else pressing a band leaves its .lbtn twin lit');
t('the legacy toggle repaints every surface it now governs',
  (body('function _ctxLayerToggle(layer){') || '').indexOf('_ctxViewRepaint();') >= 0);

// ---- ONE frame identity, and picking a frame SHOWS a turn rather than loading it ---------------------------
t('CTX_ACTIVE is gone - _ctxActiveFrame (the turn in view) is the only answer',
  (src.match(/CTX_ACTIVE/g) || []).length <= 1, 'only the comment explaining its removal may remain');
const lf = body('function loadFrame(id){');
t('loadFrame no longer assigns the globals the next question carries',
  !!lf && lf.indexOf('CTX_NODES=JSON.parse') < 0 && lf.indexOf('CTX_EDGES=JSON.parse') < 0,
  'viewing an old turn must never rewrite what you are about to send');
t('it scrolls the chat to that turn instead, as the grown graph\'s scrubber already did',
  !!lf && lf.indexOf('if(f.mid){ _ctxScrollTo(f.mid); }') >= 0);
t('the legacy strip and pane mark the turn in view',
  src.indexOf('const _on=(f)=>String(f.id)===String(_ctxActiveFrame);') >= 0);
t('frames arriving over the embed bridge reach the legacy frames UI too',
  src.indexOf('try{ _ctxFollow(true); updateFrameUI(); VeraLHM.render(); }catch(_){} } };') >= 0,
  'the embedded menu replaced CTX_FRAMES without telling them, so they sat empty all session');

// ---- the one server round-trip on a frame path stays fenced ------------------------------------------------
t('the session-restore re-query is marked as the boundary it is',
  src.indexOf('THE BOUNDARY, and it is worth being explicit about because this is the only place in the file that asks') >= 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
