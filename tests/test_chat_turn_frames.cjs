// Every turn keeps its own context, in the page (Notes/42 defect 103; vera/chat/chat_panel.html).
//
// _saveFrame already snapshots a frame per turn, so scrolling needs no fetch, no server store and no reload.
// What was broken is that a turn could end up with NO frame: the save ran at reply time, re-read live
// CTX_NODES then, and was gated on that being non-empty. By reply time the live set can hold the NEXT
// question's context or be empty, so the turn's own context was lost and scrolling fell back to the live set.
// Measured in-page: two turns whose context genuinely differed (37be4d66... then 0478794d...) produced ONE
// frame, labelled "Turn 2", and both questions drew a byte-identical 26-node graph.
// The context is now captured at SEND and handed to _saveFrame, which uses it when given.
//   node tests/test_chat_turn_frames.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// the capture happens at send, with the mids that already travel there
t('the turn carries the context it actually sent', src.indexOf('nodes:JSON.parse(JSON.stringify(CTX_NODES)), edges:JSON.parse(JSON.stringify(CTX_EDGES)),') >= 0);
t('and its turn number, taken from the rendered questions', src.indexOf("turnNo:document.querySelectorAll('#msgs .mwrap.u').length") >= 0);
t('the drifting HISTORY-based label is gone', src.indexOf("_saveFrame('Turn '+HISTORY.filter") < 0);
t('the save is gated on the SNAPSHOT, not on whatever is live later', src.indexOf('const _sn=Array.isArray(_ts.nodes)&&_ts.nodes.length?_ts.nodes:') >= 0 && src.indexOf('if(CTX_NODES.length){const _ts=_ctxTurnSend') < 0);
t('a very long session cannot grow without bound', src.indexOf('if(CTX_FRAMES.length>500)CTX_FRAMES.splice(0,CTX_FRAMES.length-500);') >= 0);

// _saveFrame itself, run for real
const F = src.slice(src.indexOf('  function _saveFrame(label'), src.indexOf('  // which frame is the turn in view'));
t('_saveFrame never reaches the server — the frames live in the page', !/api\(|fetch\(|ctxFetch\(/.test(F));

let CTX_FRAMES = [], CTX_ACTIVE = null;
const live = [{ id: 'live1', source: 'memory' }], liveE = [{ from: 'live1', to: 'live2' }];
const mkSave = () => new Function('CTX_NODES', 'CTX_EDGES', 'CTX_FRAMES', 'CTX_ACTIVE', '_ctxQuery', '_ctxBroadcastFrames',
  F + '\nreturn _saveFrame;')(live, liveE, CTX_FRAMES, CTX_ACTIVE, 'q', () => {});
const save = mkSave();

const sent = [{ id: 'a', source: 'memory' }, { id: 'b', source: 'cap' }];
const f1 = save('Turn 1', true, { mid: 'm1', amid: 'm2', nodes: sent, edges: [{ from: 'a', to: 'b' }], sentIds: new Set(['a', 'b']) });
t('a turn stores the set it handed in, not the live one', f1.nodes.length === 2 && f1.nodes[0].id === 'a' && f1.nodes[1].id === 'b');
t('and the edges it handed in', f1.edges.length === 1 && f1.edges[0].from === 'a');
t('bound to that turn on both halves', f1.mid === 'm1' && f1.amid === 'm2' && f1.turn === 'turn m1');
t('the snapshot is a COPY — later mutation of the source cannot change it', (() => { sent[0].id = 'mutated'; return f1.nodes[0].id === 'a'; })());

const f2 = save('Frames button', true, {});
t('a caller that hands in nothing still snapshots the LIVE set', f2.nodes.length === 1 && f2.nodes[0].id === 'live1' && f2.edges[0].from === 'live1');

const f3 = save('Turn 2', true, { mid: 'm3', amid: 'm4', nodes: [{ id: 'c', source: 'run' }], edges: [], sentIds: new Set([]) });
t('two turns give two DISTINCT frames — the whole point', f1.nodes[0].id !== f3.nodes[0].id && f1.mid !== f3.mid);
t("a record the question did not send is marked as the answer's", f3.nodes[0].by === 'a');

// the cap, driven through the real function
const cap = new Function('CTX_NODES', 'CTX_EDGES', 'CTX_FRAMES', 'CTX_ACTIVE', '_ctxQuery', '_ctxBroadcastFrames', F + '\nreturn {_saveFrame, CTX_FRAMES};');
const env = cap(live, liveE, [], null, 'q', () => {});
for (let i = 0; i < 520; i++) env._saveFrame('T' + i, true, { mid: 'm' + i, nodes: [{ id: 'n' + i }], edges: [] });
t('the frame list is capped at 500, oldest dropped', env.CTX_FRAMES.length === 500 && env.CTX_FRAMES[0].mid === 'm20' && env.CTX_FRAMES[499].mid === 'm519', 'len=' + env.CTX_FRAMES.length);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
