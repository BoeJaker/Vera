// Each turn's frame is bound to its message elements, so the lookup cannot drift
// (Notes/42 defect 104; vera/chat/chat_panel.html).
//
// The frames are created per turn - chips accumulate "Turn 2", "Turn 3" as turns complete - but finding the
// right one kept failing, because every identifier the lookup leaned on drifts: HISTORY turn numbers produced
// two frames both labelled "Turn 2", mids are re-issued at render time, and _ctxTurnSend is null on some
// paths. Measured over three turns, questions 1 and 2 drew a byte-identical 22-node graph while question 3
// drew a different one. The frame is now tagged onto the question and the answer, and read back off the
// element in view.
//   node tests/test_chat_frame_binding.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

t('saving a frame tags both of its messages', src.indexOf('_tag(f.mid); _tag(f.amid);') >= 0 && src.indexOf('w.dataset.frame=String(f.id)') >= 0);
t('the lookup reads the tag off the focused message', src.indexOf('const fid=w&&w.dataset.frame;') >= 0);
t('and keeps the mid/amid comparison as a fallback', src.indexOf('f=CTX_FRAMES.find(x=>x.mid===mid||x.amid===mid)||null;') >= 0);
t('the newest turn still yields to the live set while typing', src.indexOf("if(f===CTX_FRAMES[CTX_FRAMES.length-1]){ const inp=document.getElementById('chatInput');") >= 0);
const V = src.slice(src.indexOf('function _ctxFrameInView'), src.indexOf('function _ctxBroadcastFrames'));
t('the lookup never reaches the server', !/api\(|fetch\(|ctxFetch\(/.test(V));

// run the real lookup against a stubbed transcript
const CTX_FRAMES = [
  { id: 1001, mid: 'm1', amid: 'm2', nodes: [{ id: 'a' }] },
  { id: 1002, mid: 'm3', amid: 'm4', nodes: [{ id: 'b' }] },
  { id: 1003, mid: 'zzz', amid: '', nodes: [{ id: 'c' }] } ];
const wraps = { m1: { dataset: { mid: 'm1', frame: '1001' } }, m2: { dataset: { mid: 'm2', frame: '1001' } },
                m3: { dataset: { mid: 'm3', frame: '1002' } }, m4: { dataset: { mid: 'm4', frame: '1002' } },
                m9: { dataset: { mid: 'm9' } } };
const document = { querySelector: (s) => { const m = /data-mid="([^"]+)"/.exec(s); return (m && wraps[m[1]]) || null; },
                   getElementById: () => null };
const fn = src.slice(src.indexOf('  function _ctxFrameInView'), src.indexOf('  function _ctxBroadcastFrames'));
const look = new Function('CTX_FRAMES', 'document', '_ctxTypingT', fn + '\nreturn _ctxFrameInView;')(CTX_FRAMES, document, 0);

t('a question finds its own frame by tag', look('m1') && look('m1').id === 1001);
t('its ANSWER finds the same frame', look('m2') && look('m2').id === 1001);
t('the next turn finds a DIFFERENT frame — the whole point', look('m3') && look('m3').id === 1002 && look('m3').id !== look('m1').id);
t('that answer too', look('m4') && look('m4').id === 1002);
t('an untagged message still resolves by mid if a frame claims it', look('zzz') && look('zzz').id === 1003);
t('a message with no frame at all resolves to nothing', look('m9') === null);
t('an empty mid resolves to nothing', look('') === null);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
