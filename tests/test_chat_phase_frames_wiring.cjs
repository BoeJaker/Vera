// The chat turn says what it is doing: the stream sends phase frames when asked, the page adds its own for the steps
// before the request, and the throbber names the running step instead of a stock phrase.
//   node tests/test_chat_phase_frames_wiring.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), A = R('vera/agents/agents.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

t('the stream builds frames only for a client that asks', A.includes('silent=not body.get("phase_frames"))')   /* always a clock (the turn record, FM-0.3); silent unless asked */);
t('...reports the steps done before the stream opened, with their real times', A.includes('for _f in _phases.from_marks(_ph, _ep_marks):'));
t('...names the web search when it starts and when it finishes, even on the two-tier path',
  A.includes('_ph.sse("acquire", "web", _phases.web_label(_web_query), "start", t=_web_t0)') && A.includes('_web_phase = asyncio.create_task(_web_phase_done())'));
t('...marks the first token on the model stage, then the end', A.includes('yield _ph.sse("model", "answer", "writing the answer", "first")')
  && A.includes('yield _ph.sse("model", "answer", "writing the answer", "done",'));
t('...and its web watcher is torn down with the rest', A.includes('for _t in (_op_pump, _opener_task, _main_pump, _web_task, _web_phase')   /* the memory tasks joined the teardown */);
t('the page asks for them (off with vera:chat:phases = 0) and sends the turn id', H.includes("phase_frames:_phasesOn()||undefined, turn_id:_phaseTurnId(aMsg.wrap)||undefined,")
  && H.includes("localStorage.getItem('vera:chat:phases')!=='0'"));
t('...hands each frame to its message', H.includes("else if(ev.type==='phase'){ _phaseNote(aMsg.wrap, ev); }"));
t('...names its own steps before the request', ['finding the agent', 'gathering skills and context', 'checking memory for this question', 'noting what is open']
  .every(l => (H.match(new RegExp("_phaseLocal\\(aMsg\\.wrap,'[a-z]+','[a-z]+','" + l + "','start'\\)", 'g')) || []).length === 1
    && (H.match(new RegExp("_phaseLocal\\(aMsg\\.wrap,'[a-z]+','[a-z]+','" + l + "','done'\\)", 'g')) || []).length >= 1));   /* a step may end on success and on failure */
t('the stock phrases leave a phrase that names a running step alone', H.includes('if(el.dataset.phase) return;'));

// behaviour of _phaseNote, run from the page's own source
{ const s = H.indexOf('function _phaseNote(wrap, f){'), e = H.indexOf('// a step this page takes itself before the request');
  const src = H.slice(s, e);
  const phrase = { textContent: 'herding tokens', dataset: {}, classList: { remove() {}, add() {} }, offsetWidth: 0 };
  const wrap = { querySelector: () => phrase, dataset: { mid: 'm1' } };
  const ctx = { wrap, Map }; vm.createContext(ctx); vm.runInContext(src + '\nthis._phaseNote=_phaseNote;', ctx);
  const note = (f) => ctx._phaseNote(wrap, Object.assign({ type: 'phase' }, f));
  note({ stage: 'acquire', id: 'web', label: 'searching the web for "x"', state: 'start' });
  t('a running step is named on the throbber', phrase.textContent === 'searching the web for "x"…' && phrase.dataset.phase === 'acquire');
  note({ stage: 'model', id: 'answer', label: 'writing the answer', state: 'start' });
  t('...the one started last wins', phrase.textContent === 'writing the answer…');
  note({ stage: 'model', id: 'answer', label: 'writing the answer', state: 'done' });
  t('...when it ends, the one still running is named again', phrase.textContent === 'searching the web for "x"…');
  note({ stage: 'acquire', id: 'web', label: 'searching the web for "x"', state: 'done' });
  t('...and with nothing running the stock phrases may return', !phrase.dataset.phase);
  t('every frame is kept on the message', wrap._phases.length === 4); }

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
