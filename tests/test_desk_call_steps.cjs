// DA-04c (e): a call made FROM THE DESK reports its steps too - "Run again" on a result item draws the run strip on that
// item while it runs; an app's action says its latest step in the app's bar. The call carries a trace of its own and
// its `<cap>.step` events come over the page's live channel.
//   node tests/test_desk_call_steps.cjs
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const S = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

(async () => {
  // the helper, from the source, against a stand-in live channel
  const a = S.indexOf('    async _traceSteps(onStep) {'), b = S.indexOf('    async callResult(name, args, opts) {');
  const subs = []; const L = { subscribe: (f, cb) => { const s = { f, cb, closed: false, close() { this.closed = true; } }; subs.push(s); return s; }, ready: async () => true };
  const ctx = vm.createContext({ root: { VeraLive: L }, Date, String, setTimeout, Object });
  const M = vm.runInContext('({' + S.slice(a, b) + '})', ctx);
  const got = []; const tr = await M._traceSteps.call({}, (m) => got.push(m.stage));
  t('a desk call gets a trace of its own and listens on it', /^t-desk-/.test(tr.tid) && subs.length === 1 && subs[0].f.trace[0] === tr.tid);
  subs[0].cb({ type: 'netscan.lan.scan.step', stage: 'probing' }); subs[0].cb({ type: 'canvas.updated', stage: 'x' }); subs[0].cb({ event: 'catalog.pull.start.step', stage: 'downloading' });
  t('only its .step events reach it', got.join() === 'probing,downloading', got.join());
  tr.close(); await new Promise((r) => setTimeout(r, 1600));
  t('it lets go a moment after the answer', subs[0].closed === true);
  const none = await M._traceSteps.call({}, () => {}); vm.runInContext('root.VeraLive = null', ctx);
  const bare = await M._traceSteps.call({}, () => {});
  t('without a live channel it still gives a trace (no steps)', /^t-desk-/.test(bare.tid) && typeof bare.close === 'function' && !!none);

  // the calls carry it
  t('callResult and call send a trace when given one', S.includes("JSON.stringify(Object.assign({ name, arguments: args || {} }, opts && opts.trace ? { trace_id: opts.trace } : {}))") && S.includes("JSON.stringify(Object.assign({ name, arguments: Object.assign({ id: this.canvasId }, args || {}) }, opts && opts.trace ? { trace_id: opts.trace } : {}))"));
  // Run again: the strip on the item, then the answer
  t('Run again draws its steps on the item (running + run) and runs canvas.run on its trace', S.includes("this.liveUpdate(key, { running: true, run: Object.assign({}, run) }); })") && S.includes(".then((tr) => this.call('canvas.run', { cap: c.cap, args: c.args || {}, key }, { trace: tr.tid })"));
  t('...and lets the strip go when the answer is in', S.includes(".finally(() => { tr.close(); if (run.stage) { this.liveUpdate(key, { running: false }); this.liveDone(key); } })"));
  // an app's action: its latest step in the bar
  t('an app action says its step in the bar while it acts', S.includes("ui.msg = { ok: true, text: a.label + '… ' + s }; this._appDraw(key); });") && S.includes("res = await this.callResult(a.cap, appActArgs(a, row, appArgs, vals), { trace: tr.tid });"));
  // the result element draws the strip for a running item (DA-04c (a))
  const V = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
  const h = V.BLOCK.result({ cap: 'netscan.lan.scan', args: {}, result: { alive: [] }, running: true, run: { stage: 'probing', done: 5, total: 10 } }, 'm', 'r1', {});
  t('a result item running again shows the strip over its old answer', /data-as="running"/.test(h) && /5 of 10/.test(h));
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
