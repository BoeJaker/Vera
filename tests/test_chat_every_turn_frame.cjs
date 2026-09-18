// Every completed turn gets a frame, tagged onto its messages from the DOM
// (Notes/42 defect 105; vera/chat/chat_panel.html).
//
// Measured before this, over three turns: tagged m1:- m2:- m3:<id> m4:<id> m5:- m6:- - only one turn in three
// ended up with a frame on its messages, and the two without fell back to the LIVE set, so all three questions
// drew one identical graph. Two conditions caused it: the save was skipped when a turn carried no records, and
// the tagging looked the message up by mid, which drifts.
// Now: the save is unconditional (an empty frame is the truthful "this turn had no context", and it stops a
// turn borrowing the live set), and the tag is written from the assistant message object in hand plus the
// .mwrap immediately before it - no mid, no ordinal.
//   node tests/test_chat_every_turn_frame.cjs   (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'chat', 'chat_panel.html'), 'utf8');

t('the save is unconditional — every completed turn gets a frame', src.indexOf('const _f=_saveFrame(') >= 0 && src.indexOf('if(_sn){ _saveFrame(') < 0);
t('a turn with no records still yields a frame, with an empty node list', src.indexOf('CTX_NODES.length?CTX_NODES.slice():[]') >= 0);
t('the answer is tagged from the message object in hand', src.indexOf('_aw.dataset.frame=String(_f.id);') >= 0);
t('the question is found as the .mwrap before it, not by mid', src.indexOf("_qw.classList.contains('u')") >= 0);
const B = src.slice(src.indexOf('const _f=_saveFrame('), src.indexOf('updateFrameUI();', src.indexOf('const _f=_saveFrame(')));
t('nothing in the tagging path looks a message up by mid', !/querySelector\(.*data-mid/.test(B));
t('the lookup still reads the tag off the focused message', src.indexOf('const fid=w&&w.dataset.frame;') >= 0);

// the walk back to the question, run for real against a stubbed transcript
const mk = (cls) => ({ classList: { contains: (c) => cls.split(' ').includes(c) }, dataset: {}, previousElementSibling: null });
const q = mk('mwrap u'), a = mk('mwrap a'), stray = mk('audio-row');
stray.previousElementSibling = q; a.previousElementSibling = stray;   // a non-mwrap node sits between them
const walk = (aw) => { let w = aw.previousElementSibling;
  while (w && !(w.classList && w.classList.contains('mwrap'))) w = w.previousElementSibling;
  return (w && w.classList.contains('u')) ? w : null; };
t('the walk skips non-message nodes and finds the question', walk(a) === q);
const orphan = mk('mwrap a'); orphan.previousElementSibling = null;
t('an answer with no question before it tags nothing, and does not throw', walk(orphan) === null);
const afterA = mk('mwrap a'); afterA.previousElementSibling = mk('mwrap a');
t('an answer preceded by another answer is not mistaken for a question', walk(afterA) === null);

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
