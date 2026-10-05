// The last turn's steps: a reply's phase frames become one row per step for the chat:turn-phases widget (gantt
// form), shown in the Context menu's "Last turn" pane - never in the transcript (owner, 2026-10-05).
//   node tests/test_turn_footer.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const H = R('vera/chat/chat_panel.html'), W = R('vera/widgets/widget_registry.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };

const a = H.indexOf('function _phaseRows(wrap){'), b = H.indexOf('// a reply ended: keep its steps');
t('the row builder and the turn hand-off are on the page', a > 0 && b > a);
const _phaseRows = new Function(H.slice(a, b) + '; return _phaseRows;')();

// the bitcoin turn on prod (EVIDENCE E14), with the page's own steps before the request
const P = (o) => Object.assign({ type: 'phase', turn_id: 't1' }, o);
const wrap = {
  _phaseT0: 1000, _phaseSentAt: 1300,
  _phases: [
    P({ id: 'agent', stage: 'needs', label: 'finding the agent', state: 'start', at_ms: 0, local: true }),
    P({ id: 'agent', stage: 'needs', label: 'finding the agent', state: 'done', at_ms: 40, ms: 40, local: true }),
    P({ id: 'memory', stage: 'acquire', label: 'checking memory for this question', state: 'start', at_ms: 60, local: true }),
    P({ id: 'memory', stage: 'acquire', label: 'checking memory for this question', state: 'done', at_ms: 260, ms: 200, local: true }),
    P({ id: 'web', stage: 'needs', label: 'deciding whether to search the web', state: 'done', at_ms: 25, ms: 0, detail: 'price' }),
    P({ id: 'memory', stage: 'needs', label: 'deciding whether to search earlier conversations', state: 'skip', at_ms: 25, ms: 0 }),
    P({ id: 'web', stage: 'acquire', label: 'searching the web', state: 'start', at_ms: 24 }),
    P({ id: 'web', stage: 'acquire', label: 'searching the web', state: 'done', at_ms: 1925, ms: 1901, count: 5 }),
    P({ id: 'answer', stage: 'model', label: 'writing the answer', state: 'start', at_ms: 1930 }),
    P({ id: 'answer', stage: 'model', label: 'writing the answer', state: 'first', at_ms: 3612 }),
    P({ id: 'answer', stage: 'model', label: 'writing the answer', state: 'done', at_ms: 8400, ms: 6470, count: 864 }),
  ],
};
const ph = _phaseRows(wrap);
t('one row per step that ran; a skipped step is named, not drawn', ph && ph.rows.length === 5 && ph.skipped.join() === 'deciding whether to search earlier conversations');
const web = ph.rows.find((r) => r.id === 'acquire:web');
t('server frames are shifted by when the request went (300 ms after the turn began)', web && web.start === 324 && web.end === 2225);
t('a row names its step, its time and what it found', web && web.name === 'searching the web · 1.9 s · 5 found');
t('the answer counts its characters', ph.rows.find((r) => r.id === 'model:answer').name === 'writing the answer · 6.5 s · 864 chars');
t('the page\'s own steps keep their own clock', ph.rows[0].id === 'needs:agent' && ph.rows[0].start === 0 && ph.rows[0].end === 40);
t('rows are in the order the steps started', ph.rows.every((r, i, a) => !i || a[i - 1].start <= r.start));
t('the first word is on the turn\'s clock', ph.first === 3912);
t('the turn\'s length is the last step\'s end', ph.total === 8700);
t('a finished step is drawn done', web.state === 'done');
const z = ph.rows.find((r) => r.id === 'needs:web');
t('a zero-length step still has a visible bar', z && z.end > z.start);

const late = _phaseRows({ _phases: [P({ id: 'web', stage: 'acquire', label: 'searching the web', state: 'start', at_ms: 20 }),
  P({ id: 'web', stage: 'acquire', label: 'searching the web', state: 'late', at_ms: 3020, ms: 3000 })] });
t('a search past its cap is drawn in the warning colour', late && /var\(--warn, #[0-9a-f]{6}\)/.test(late.rows[0].col || ''));
const failed = _phaseRows({ _phases: [P({ id: 'memory', stage: 'acquire', label: 'x', state: 'fail', at_ms: 10, ms: 10 })] });
t('a failed step is drawn failed', failed && failed.rows[0].state === 'failed');
t('only the page\'s own frames: no footer (the server said nothing)', _phaseRows({ _phases: [P({ id: 'a', stage: 'needs', state: 'done', at_ms: 1, local: true })] }) === null);
t('no frames: no footer', _phaseRows({}) === null && _phaseRows(null) === null);

t('the footer draws through the registered template, never by hand', H.includes("w.record={ id:'chat:turn-phases', form:'gantt'") && W.includes('"id": "chat:turn-phases", "name": "Turn phases", "form": "gantt"'));
t('nothing is put beside a reply: the transcript holds no step list', !H.includes('_phaseFooter') && !H.includes("className='turn-phases'") && !H.includes("insertAdjacentElement('afterend', det)"));
t('the steps are a pane of the Context menu', H.includes("{ id:'paneTurnPhases', n:'Last turn', tab:'ctx', cls:'dk-list', load:()=>_turnPhasesRender() }") && H.includes('<div id="paneTurnPhases" class="ctx-pane"></div>'));
t('the split menu instance is told the last turn', H.includes("_lhmPost('turn', _lastTurnPhases)") && H.includes("else if(m.act==='turn'){ _lastTurnPhases=a&&a.ph?a:null; try{ _turnPhasesRender(); }catch(_){} }"));
t('the steps are kept when the reply is finished', H.includes('try{ _phaseTurnDone(aMsg.wrap); }catch(_){}'));
t('the request time is noted right before the stream is requested',
  /aMsg\.wrap\._phaseSentAt=performance\.now\(\);[^\n]*\r?\n\s*const resp=await fetch\(BASE\+'\/agents\/chat\/stream'/.test(H));
const E = R('vera/widgets/widget_element.js');
t('the gantt name column gives way in a narrow widget (never past 45% of its width)', E.includes('Math.min(260, +o.draw.names, (o.width > 0 ? Math.floor(o.width * 0.45) : 260))'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
