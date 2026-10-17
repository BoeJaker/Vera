// DA-04c: the run element - a call that reports its steps is ONE item (a `result` keyed run:<trace>) that shows how it
// goes and becomes its answer in place; it leaves the activity line and the reply's harvest; the lookup, the research run
// and its pages are on the same live-item core.
//   node tests/test_run_element.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const R = path.join(__dirname, '..');
const H = fs.readFileSync(path.join(R, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const V = require(path.join(R, 'vera', 'canvas', 'canvas_element.js'));
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const cut = (a, b) => { const i = H.indexOf(a), j = H.indexOf(b, i + 1); if (i < 0 || j < 0) throw new Error('anchor: ' + (i < 0 ? a : b)); return H.slice(i, j); };

// the canvas: a running result draws its strip, then its answer
{ const s = V.runStrip({ stage: 'probing', done: 64, total: 254, found: 7, text: '192.168.0.0/24', started: Date.now() - 4000 });
  t('the strip: stage, words, n of m, found, seconds, a bar', /<b>probing<\/b>/.test(s) && /192\.168\.0\.0\/24/.test(s) && /64 of 254 · 7 found · [45] s/.test(s) && /width:25%/.test(s));
  t('...no bar without a total', !/vc-run-bar/.test(V.runStrip({ stage: 'starting' }))); }
{ const el = {}; const h = V.BLOCK.result({ cap: 'netscan.lan.scan', args: { cidr: '10.0.0.0/24' }, running: true, run: { stage: 'probing', done: 3, total: 10 } }, 'm', 'run:t1', el);
  t('a running result: the strip and a skeleton, no answer yet', /data-as="running"/.test(h) && /vc-run-bar/.test(h) && /vc-app-psk/.test(h));
  t('...its line says it is running', /running/.test((el._decl || {})['run:t1'].sub || ''));
  const done = V.BLOCK.result({ cap: 'netscan.lan.scan', args: {}, ok: true, ms: 4200, result: { alive: [{ ip: '10.0.0.1', hostname: 'a' }, { ip: '10.0.0.2', hostname: 'b' }], count: 2 } }, 'm', 'run:t1', el);
  t('when it ends the same item draws the answer', !/data-as="running"/.test(done) && /10\.0\.0\.2/.test(done));
  const bad = V.BLOCK.result({ cap: 'netscan.lan.scan', ok: false, error: 'no route', running: true }, 'm', 'run:t1', el);
  t('a failed run shows its error (not a strip)', /vc-res-err/.test(bad) && /no route/.test(bad) && !/vc-run-bar/.test(bad)); }

// the chat: the core + the run, driven by steps and the answer
const calls = [], live = [];
const cv = { liveUpdate: (k, c) => live.push([k, JSON.parse(JSON.stringify(c))]), liveDone: () => {}, refresh: () => {}, _rev: 1 };
const ctx = { SID: 's1', document: { querySelector: () => cv }, setTimeout, clearTimeout, Promise, Date, Object, String, JSON,
  _capCall: async (n, a) => { calls.push([n, JSON.parse(JSON.stringify(a))]); return { ok: true }; }, _cvEnrichOn: () => true, _cvActMid: () => 'm7', _cvAct: {}, writes: 0 };
vm.createContext(ctx);
vm.runInContext(cut('  function _cvLive(o){', '  const _CV_LOOK_RE=') + cut('  /* a call that has its own run item leaves', '  function _cvEnrichToggle(') + ';function _cvActWrite(){ writes++; }; this.api={ _cvRunStep, _cvRunEndCall, _cvActDrop, _cvRuns };', ctx);
const A = ctx.api; const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const el = {}; ctx._cvAct.m7 = { calls: [] }; const act = { mid: 'm7', call: { cap: 'netscan.lan.scan', status: 'running' } }; ctx._cvAct.m7.calls.push(act.call, { cap: 'other' });
  A._cvRunStep(el, 'netscan.lan.scan', { cidr: '10.0.0.0/24' }, 'tr1', act, { type: 'netscan.lan.scan.step', stage: 'starting', total: 254, text: '10.0.0.0/24' });
  await wait(20);
  const add = calls.find((c) => c[0] === 'canvas.add');
  t('the first step makes one result item keyed by the call', !!add && add[1].kind === 'result' && add[1].key === 'run:tr1' && add[1].content.running === true && add[1].content.run.stage === 'starting' && add[1].anchor.from === 'run');
  t('...the call is marked for the harvest to skip', el.__cvRunKey === 'run:tr1');
  t('...and leaves the activity line (the others stay)', ctx._cvAct.m7.calls.length === 1 && ctx._cvAct.m7.calls[0].cap === 'other' && act.dropped === true);
  A._cvRunStep(el, 'netscan.lan.scan', {}, 'tr1', act, { stage: 'probing', done: 100, total: 254, found: 4 });
  A._cvRunStep(el, 'netscan.lan.scan', {}, 'tr1', act, { stage: 'probing', done: 200, total: 254, found: 9 });
  t('steps are drawn live, not written each time', live.length === 2 && live[1][1].run.done === 200 && live[1][1].run.found === 9 && calls.filter((c) => c[0] === 'canvas.update').length === 0);
  t('one item only (no second add)', calls.filter((c) => c[0] === 'canvas.add').length === 1);
  const ended = A._cvRunEndCall('tr1', true, { alive: [{ ip: '10.0.0.1' }], count: 1 }); await wait(20);
  const up = calls.filter((c) => c[0] === 'canvas.update').pop();
  t('the answer goes into the same item', ended === true && !!up && up[1].key === 'run:tr1' && up[1].content.ok === true && up[1].content.result.count === 1 && !up[1].content.running && up[1].content.run.state === 'done' && typeof up[1].content.ms === 'number');
  const n = calls.length; A._cvRunStep(el, 'netscan.lan.scan', {}, 'tr1', act, { stage: 'done', done: 254, total: 254 }); await wait(10);
  t('a step after the answer changes nothing', calls.length === n);
  // a call that never reports a step: no item, its activity line kept; a step trailing its answer makes none
  t('no steps: no item', A._cvRunEndCall('tr2', true, { ok: true }) === false);
  A._cvRunStep({}, 'x.y', {}, 'tr2', null, { stage: 'late' }); await wait(10);
  t('...and a step trailing its answer makes none', !calls.some((c) => c[1].key === 'run:tr2'));
  // a failure stays in the item
  A._cvRunStep({}, 'netscan.docker.scan', {}, 'tr3', null, { stage: 'hosts', done: 1, total: 3 }); await wait(10);
  A._cvRunEndCall('tr3', false, { ok: false, error: 'engine down' }); await wait(20);
  const up3 = calls.filter((c) => c[1].key === 'run:tr3' && c[0] === 'canvas.update').pop();
  t('a failed run keeps its item with the error', !!up3 && up3[1].content.ok === false && up3[1].content.error === 'engine down' && up3[1].content.run.state === 'error');

  // wiring and the shared core
  t('every other call listens on its trace for .step events', H.includes("else if(window.VeraLive&&_cvActOn(capName)){ try{ _lookSub=VeraLive.subscribe({ trace:[_tid] }, (m)=>{ const t=String(m.event||m.type||''); if(/\\.step$/.test(t)) _cvRunStep(el, capName, coercedArgs, _tid, _act, m); }); await VeraLive.ready(400);"));
  t('its answer goes to its run item; a job keeps its listener till its last step', H.includes("if(!_look){ const sub=_lookSub; _runEnd=_cvRunEndCall(_tid, !bad, c!=null?c:(res&&(res.detail||res.error))||'no answer', ()=>{ try{ if(sub) sub.close(); }catch(_){} }); }") && H.includes("if(_lookSub&&_runEnd!=='pending') setTimeout("));

  // a job the call started (a model pull): the receipt keeps the item running; its steps go on; the last step ends it
  { let closed = 0; const el = {};
    A._cvRunStep(el, 'catalog.pull.start', { model: 'qwen3:1.7b' }, 'tj', null, { stage: 'queued', text: 'qwen3:1.7b' }); await wait(10);
    const r = A._cvRunEndCall('tj', true, { ok: true, pull_id: 'p1', pull: { state: 'queued' } }, () => { closed++; }); await wait(10);
    const upJ = () => calls.filter((c) => c[1].key === 'run:tj' && /canvas\.(add|update)/.test(c[0])).pop();
    t('a job receipt: the item waits (pending), still running, listener kept', r === 'pending' && closed === 0 && !calls.some((c) => c[0] === 'canvas.update' && c[1].key === 'run:tj'));
    A._cvRunStep(el, 'catalog.pull.start', {}, 'tj', null, { stage: 'downloading', done: 400, total: 1000, text: 'qwen3:1.7b · 12 MB/s' });
    t('...its steps go on after the answer', live.filter((x) => x[0] === 'run:tj').pop()[1].run.done === 400 && live.filter((x) => x[0] === 'run:tj').pop()[1].running === true);
    A._cvRunStep(el, 'catalog.pull.start', {}, 'tj', null, { type: 'catalog.pull.start.step', trace_id: 'tj', stage: 'done', done: 1000, total: 1000, model: 'qwen3:1.7b', total_bytes: 1000 }); await wait(20);
    const fin = upJ();
    t('...its last step ends it: the answer in the item, the listener let go', !!fin && fin[1].content.ok === true && fin[1].content.running === false && fin[1].content.result.model === 'qwen3:1.7b' && fin[1].content.result.pull_id === 'p1' && !('trace_id' in fin[1].content.result) && closed === 1); }
  { const el = {}; A._cvRunStep(el, 'catalog.pull.start', {}, 'tk', null, { stage: 'queued' }); A._cvRunEndCall('tk', true, { pull_id: 'p2' }); await wait(5);
    A._cvRunStep(el, 'catalog.pull.start', {}, 'tk', null, { stage: 'error', text: 'node unreachable' }); await wait(20);
    const f = calls.filter((c) => c[1].key === 'run:tk' && c[0] === 'canvas.update').pop();
    t('...a job that fails ends with its error', !!f && f[1].content.ok === false && /node unreachable/.test(f[1].content.error)); }
  t('...and one that goes quiet ends after 30 minutes', H.includes("g.idle=setTimeout(()=>_cvRunFinish(g, false, 'no news from it for 30 minutes'), 30*60*1000)"));
  t('the harvest skips a call with a run item', H.includes("if(el.__cvRunKey) return;"));
  t('the lookup is on the core', H.includes("const L=h.L||(h.L=_cvLive({ key:h.key, kind:'records', size:'l', mid:h.mid, from:'lookup', content:()=>h.final||_cvLookContent(h) }));"));
  t('the research run is on the core', H.includes("r.L=_cvLive({ key:r.key, kind:'research', size:'l', mid, content:(keep)=>_cvRunContent(jobId, r, keep), onAdded:"));
  t('its pages list is on the core', H.includes("g.L=g.L||_cvLive({ key:g.key, kind:'records', size:'l', mid, content:()=>g.content });"));
  t('no item code of its own left (one add path for live items)', (H.match(/_capCall\('canvas\.add',\{ session_id:SID, kind:L\.kind/g) || []).length === 1 && !/\.landed\b/.test(H));
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
