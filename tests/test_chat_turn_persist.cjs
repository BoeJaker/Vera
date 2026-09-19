// A recorded turn survives a reload (Notes/42 defect 107; vera/chat/chat_panel.html).
//
// Turns are recorded in the page, but the page dies. This stores each recorded turn in the notes KV - chosen
// because it is keyed and reads back immediately, where /memory/search cannot see freshly written rows at all
// (0 results at t+0s, t+28s and t+45s for a row whose write reported three green backends).
// THE KEY IS THE QUESTION'S OWN WORDS, never a counter: HISTORY user counts gave two frames both called
// "Turn 2", mids are reissued at render time, and ordinals shift when a restore truncates.
//   node tests/test_chat_turn_persist.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

const S = src.slice(src.indexOf('  const _CTXK_SCOPE='), src.indexOf('  function _ctxBroadcastFrames(){'));
t('the store never touches what the next question carries', !/CTX_NODES\s*=|CTX_EDGES\s*=|ctxFetch\s*\(/.test(S));
t('it does not use the search index, which cannot see fresh rows', !/\/memory\/(store|search)/.test(S));
t('it uses the keyed notes store', S.indexOf("'/notes/set'") >= 0 && S.indexOf('/notes/get?scope=') >= 0);
t('a recorded turn is written as it is recorded', src.indexOf('_ctxTurnPersist(_qw,_f);') >= 0);
t('and read back after the transcript exists, so questions can be keyed', src.indexOf('await _ctxTurnsRestore(sid);') >= 0);
t('a question that already has a frame is never overwritten', S.indexOf('if(qw.dataset.frame) return;') >= 0);

const M = new Function('document', 'api', 'SID', '_saveFrame', 'updateFrameUI', '_ctxBroadcastFrames', '_ctxFollow',
  S + '\nreturn {_ctxTurnKey,_ctxTurnPack,_ctxTurnPersist,_ctxTurnsRestore,_ctxHash};');

const mkQ = (txt) => ({ dataset: {}, classList: { contains: (c) => c === 'mwrap' || c === 'u' },
  querySelector: () => ({ textContent: txt }), nextElementSibling: null });
const q1 = mkQ('what is a tide'), q2 = mkQ('what is a compiler'), q3 = mkQ('what is a tide');
const doc = { getElementById: () => ({ querySelectorAll: () => [q1, q2, q3] }) };
let posted = [];
const api = (p, m, b) => { posted.push({ p, m, b }); return Promise.resolve({ exists: false }); };
const env = M(doc, api, 'sess-1', () => ({ id: 7 }), () => {}, () => {}, () => {});

const k1 = env._ctxTurnKey('sess-1', q1), k2 = env._ctxTurnKey('sess-1', q2), k3 = env._ctxTurnKey('sess-1', q3);
t('the key comes from the question text, not a counter', k1 && k2 && k1 !== k2 && !/:t\d+$/.test(k1));
t('the same question asked twice gets distinct keys by occurrence', k1 !== k3 && k3.indexOf('#1') > 0, k1 + ' vs ' + k3);
t('the key is stable for the same question', env._ctxTurnKey('sess-1', q1) === k1);
t('a different session gives a different key', env._ctxTurnKey('sess-2', q1) !== k1);

const packed = JSON.parse(env._ctxTurnPack({ query: 'q'.repeat(400),
  nodes: [{ id: 'a', label: 'L'.repeat(300), source: 'memory', score: 0.9, text: 'BODY'.repeat(200) }, { id: 'b', source: 'cap', included: false }],
  edges: [{ from: 'a', to: 'b', type: 'CITES' }] }));
t('labels are capped and record BODIES are never stored', packed.n[0].l.length === 80 && JSON.stringify(packed).indexOf('BODY') < 0);
t('the query is capped and edges keep their type', packed.q.length === 160 && packed.e[0].r === 'CITES');
t('an excluded record stays excluded', packed.n[1].x === 0);
const big = env._ctxTurnPack({ query: 'q', nodes: Array.from({ length: 400 }, (_, i) => ({ id: 'n' + i, label: 'x'.repeat(80), score: i })), edges: [] });
t('an oversized turn is trimmed to fit the note cap, not dropped', big.length <= 5600 && JSON.parse(big).n.length >= 4);

posted = []; env._ctxTurnPersist(q1, { nodes: [{ id: 'a' }], edges: [] });
t('persisting writes one keyed note', posted.length === 1 && posted[0].p === '/notes/set' && posted[0].b.scope === 'ctxturn' && posted[0].b.ref_id === k1);
posted = []; env._ctxTurnPersist(q1, { nodes: [] }); env._ctxTurnPersist(null, { nodes: [{ id: 'a' }] });
t('a turn with no records, or no question, is not written', posted.length === 0);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
