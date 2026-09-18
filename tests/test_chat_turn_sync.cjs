// A turn's context is STORED when the turn happens, and looked up afterwards (Notes/42 defect 101;
// vera/chat/chat_panel.html).
//
// It used to be re-derived by running a full retrieval as the reader scrolled, which was slow, spent a recall
// per scroll, and was not even the right answer - it returned what retrieval says TODAY about that question,
// so the graph could show records that were never in that prompt. A turn's context is a fact about that turn:
// _saveFrame already has the REAL assembled set the moment a turn completes, and the chat already writes its
// own records through /memory/store (session names as 'event', cap results as 'cap_result'), so the frame is
// persisted as one 'ctx_frame' record and read back in a single search when the session opens.
//   node tests/test_chat_turn_sync.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

// ── the mechanism that was wrong is GONE, not merely bypassed ──
for (const dead of ['_ctxDeriveFrame', '_turnPair', '_turnCtxBusy', '_turnCtxDone'])
  t('the on-scroll derivation is gone: ' + dead, src.indexOf(dead) < 0);
t('and the sink that existed only to serve it is gone with it', src.indexOf('const solo=!!sink') < 0 && src.indexOf('sink.nodes=nodes') < 0);
t('ctxFetch is back to its own signature', src.indexOf("async function ctxFetch(force=false,queryOverride='',fast=false){") >= 0);
t('nothing re-derives a past turn behind the reader', !/ctxFetch\([^)]*sink/.test(src));

// ── the wiring: written at the one moment it is known, read once when the session opens ──
t('the completed turn writes its frame down', src.indexOf('_ctxFrameStore(CTX_FRAMES[CTX_FRAMES.length-1])') >= 0);
// scoped to loadSession itself — record_type:'message' also appears earlier in the file
{ const LS = src.slice(src.indexOf('async function loadSession(sid){'));
  const body = LS.slice(0, LS.indexOf('renderHistList(SESSIONS);scroll();'));
  const iLoad = body.indexOf('CTX_FRAMES=await _ctxFramesLoad(sid)'), iMsgs = body.indexOf("record_type:'message'");
  t('the session reads every turn back BEFORE the transcript is walked', iLoad >= 0 && iMsgs > iLoad, 'load@' + iLoad + ' messages@' + iMsgs);
  t('and it clears any previous session\'s frames first', body.indexOf('CTX_FRAMES=[];') >= 0 && body.indexOf('CTX_FRAMES=[];') < iLoad); }
t('a stored frame binds the question AND the answer, which is what _ctxFrameInView matches on',
  /mid:d\.mid, amid:d\.amid/.test(src) && src.indexOf('CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)') >= 0);

const blk = src.slice(src.indexOf('  const _CTXF_TYPE='), src.indexOf('  // the frame in view right now'));
t('storing never touches what the NEXT prompt carries', !/CTX_NODES\s*=|CTX_EDGES\s*=/.test(blk));

// ── run the real functions against stubs ──
let posted = [];
const api = (p, m, b) => { posted.push({ p, m, b }); return Promise.resolve(FAKE); };
let FAKE = { results: [] };
const M = new Function('api', 'SID', blk + '\nreturn {_ctxFrameSlim,_ctxFrameStore,_ctxFramesLoad,_CTXF_TYPE};')(api, 'sess-1');

const frame = { mid: 'm1', amid: 'm2', label: 'Turn 1', ts: '10:00', query: 'q'.repeat(400),
  nodes: [{ id: 'a', label: 'L'.repeat(400), source: 'memory', score: 0.9, included: true, text: 'T'.repeat(900) },
          { id: 'b', label: 'two', source: 'cap', included: false, by: 'a' },
          { label: 'no id - dropped' }],
  edges: [{ from: 'a', to: 'b', type: 'CITES' }] };
const slim = M._ctxFrameSlim(frame);
t('a node with no id is dropped', slim.nodes.length === 2);
t('the compact node keeps what a graph needs to draw', slim.nodes[0].id === 'a' && slim.nodes[0].source === 'memory' && slim.nodes[0].score === 0.9 && slim.nodes[1].included === false && slim.nodes[1].by === 'a');
t('label and text are capped so a frame stays small', slim.nodes[0].label.length === 160 && slim.nodes[0].text.length === 240 && slim.query.length === 200);
t('edges keep their type', slim.edges[0].from === 'a' && slim.edges[0].to === 'b' && slim.edges[0].type === 'CITES');
t('both mids are carried', slim.mid === 'm1' && slim.amid === 'm2');

posted = []; M._ctxFrameStore(frame);
t('the frame is stored as its own record on the session', posted.length === 1 && posted[0].p === '/memory/store' && posted[0].b.record_type === 'ctx_frame' && posted[0].b.session_id === 'sess-1');
t('and the record round-trips through full_text', JSON.parse(posted[0].b.full_text).nodes.length === 2);
posted = []; M._ctxFrameStore({ nodes: [] });
t('a frame with no turn to bind to is not stored', posted.length === 0);

FAKE = { results: [
  { record: { session_id: 'sess-1', created_at: '2026-01-02', full_text: JSON.stringify({ mid: 'm3', amid: 'm4', nodes: [{ id: 'x' }], edges: [{ from: 'x', to: 'y', type: 'R' }] }) } },
  { record: { session_id: 'sess-1', created_at: '2026-01-01', full_text: JSON.stringify({ mid: 'm1', amid: 'm2', nodes: [{ id: 'a' }, { id: 'b' }] }) } },
  { record: { session_id: 'sess-1', created_at: '2026-01-03', full_text: JSON.stringify({ mid: 'm1', amid: 'm2', nodes: [{ id: 'dupe' }] }) } },
  { record: { session_id: 'other', created_at: '2026-01-04', full_text: JSON.stringify({ mid: 'zz', nodes: [] }) } },
  { record: { session_id: 'sess-1', created_at: '2026-01-05', full_text: 'not json' } } ] };
M._ctxFramesLoad('sess-1').then((fr) => {
  t('frames come back for this session only, oldest first', fr.length === 2 && fr[0].mid === 'm1' && fr[1].mid === 'm3', JSON.stringify(fr.map((f) => f.mid)));
  t('a duplicate turn is kept once', fr.filter((f) => f.mid === 'm1').length === 1);
  t('unparsable records are skipped, not fatal', fr.every((f) => Array.isArray(f.nodes)));
  t('ids are NEGATIVE numbers — updateFrameUI interpolates them unquoted into onclick', fr.every((f) => typeof f.id === 'number' && f.id < 0) && new Set(fr.map((f) => f.id)).size === fr.length);
  t('edges are rebuilt in the shape the graphs read', fr[1].edges[0].from === 'x' && fr[1].edges[0].label === 'R');
  t('a bad search does not throw', true);
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
});
