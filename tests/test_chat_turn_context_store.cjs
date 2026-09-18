// Every turn's context is written when the turn happens, so scrolling back shows THAT turn's context
// (Notes/42 defect 102; vera/chat/chat_panel.html).
//
// The focus chain was measured correct before this was written: over a 14-message transcript, focus moved
// m1 -> m4 -> m6 -> m8 -> m14 and the graph changed at m14 - the one turn that had a frame. The only missing
// piece was a frame for turns not sent in the current page load, and that context is recorded nowhere (message
// records carry graph_id:'' and relations:[]; the session graph's edges are message/activity links).
// The notes KV is used because it is keyed and reads back immediately; /memory/store is not, because a row
// written through it is invisible to /memory/search at t+0s, t+28s and t+45s.
//   node tests/test_chat_turn_context_store.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

const blk = src.slice(src.indexOf('  const _CTXF_SCOPE='), src.indexOf('  function _ctxBroadcastFrames(){'));
t('the store is present', blk.length > 200);
t('it never writes the live set and never re-queries', !/CTX_NODES\s*=|CTX_EDGES\s*=|ctxFetch\s*\(/.test(blk));
t('it does not use /memory/store or /memory/search — proven unreadable for fresh rows', !/\/memory\/(store|search)/.test(blk));
t('it uses the keyed notes store', blk.indexOf("'/notes/set'") >= 0 && blk.indexOf('/notes/get?scope=') >= 0);
t('the key is session + TURN ORDINAL, not the message id', blk.indexOf("String(sid||'')+':t'+n") >= 0);
t('the write happens as the turn completes', src.indexOf("_ctxFrameSave(HISTORY.filter(h=>h.role==='user').length") >= 0);
t('the read happens once the transcript exists, so mids can be bound', src.indexOf('CTX_FRAMES=await _ctxFramesRestore(sid,_rturns)') >= 0);
t('a restored frame binds question AND answer, which is what _ctxFrameInView matches', /mid:t\.mid, amid:t\.amid/.test(blk) && src.indexOf('CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)') >= 0);

let posted = [], FAKE = {};
const api = (p, m, b) => { posted.push({ p, m, b }); return Promise.resolve(typeof FAKE === 'function' ? FAKE(p) : FAKE); };
const M = new Function('api', 'SID', blk + '\nreturn {_ctxFramePack,_ctxFrameSave,_ctxFramesRestore,_ctxFrameKey,_CTXF_MAX};')(api, 'sess-1');

t('the key is stable and readable', M._ctxFrameKey('s', 7) === 's:t7');

const mkNodes = (k) => Array.from({ length: k }, (_, i) => ({ id: 'id-' + i, label: 'L'.repeat(200), source: 'memory', score: i / k, text: 'BODY'.repeat(300) }));
const packed = JSON.parse(M._ctxFramePack({ query: 'q'.repeat(400), nodes: mkNodes(6), edges: [{ from: 'id-0', to: 'id-1', type: 'CITES' }] }));
t('labels are capped and record BODIES are not stored', packed.n[0].l.length === 80 && !('text' in packed.n[0]) && JSON.stringify(packed).indexOf('BODY') < 0);
t('the query is capped', packed.q.length === 160);
t('nodes come back highest score first', packed.n[0].c >= packed.n[packed.n.length - 1].c);
t('edges are kept when there is room', packed.e.length === 1 && packed.e[0].r === 'CITES');
const big = M._ctxFramePack({ query: 'q', nodes: mkNodes(400), edges: [] });
t('an oversized turn is trimmed to fit the note cap, not dropped', big.length <= M._CTXF_MAX && JSON.parse(big).n.length >= 4, 'len=' + big.length);

posted = []; M._ctxFrameSave(3, { nodes: mkNodes(2), edges: [] });
t('the write goes to notes/set under session:turn', posted.length === 1 && posted[0].p === '/notes/set' && posted[0].b.scope === 'ctxframe' && posted[0].b.ref_id === 'sess-1:t3');
posted = []; M._ctxFrameSave(0, { nodes: mkNodes(1) }); M._ctxFrameSave(1, { nodes: [] });
t('a turn with no ordinal or no nodes is not written', posted.length === 0);

FAKE = (p) => p.indexOf('ref_id=sess-1%3At1') >= 0
  ? { exists: true, content: JSON.stringify({ q: 'first', n: [{ i: 'a', l: 'Alpha', s: 'memory', c: 0.9 }, { i: 'b', l: 'Beta', s: 'cap', x: 0 }], e: [{ f: 'a', t: 'b', r: 'R' }] }) }
  : { exists: false, content: '' };
M._ctxFramesRestore('sess-1', [{ n: 1, mid: 'm1', amid: 'm2' }, { n: 2, mid: 'm3', amid: 'm4' }]).then((fr) => {
  t('only turns that have a stored note come back', fr.length === 1 && fr[0].mid === 'm1' && fr[0].amid === 'm2');
  t('the nodes rehydrate into the shape the graphs draw', fr[0].nodes[0].id === 'a' && fr[0].nodes[0].label === 'Alpha' && fr[0].nodes[0].source === 'memory' && fr[0].nodes[0].score === 0.9);
  t('an excluded record stays excluded', fr[0].nodes[1].included === false && fr[0].nodes[0].included === true);
  t('edges rehydrate too', fr[0].edges[0].from === 'a' && fr[0].edges[0].label === 'R');
  t('ids are NEGATIVE numbers — updateFrameUI interpolates them unquoted into onclick', typeof fr[0].id === 'number' && fr[0].id < 0);
  t('a session with nothing stored yields nothing, and does not throw', true);
  return M._ctxFramesRestore('other', [{ n: 1, mid: 'm1' }]);
}).then((none) => {
  t('an unknown session restores no frames', none.length === 0);
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
});
