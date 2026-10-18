// Wave 1 (panel-derived): an obs.events answer opens as the Observe panel's own event stream (<vera-live-event-stream>, the widget
// element form) and starts with the answer's events (seed), at their own times, before the live ones.
'use strict';
const fs = require('fs'); const path = require('path'); const vm = require('vm'); const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
let bad = 0; const t = (name, ok) => { if (!ok) bad++; console.log((ok ? 'ok   ' : 'FAIL ') + name); };
const D = JSON.parse(R('tests/fixtures/presentation_declared.json'))['obs.events'] || {};
t('obs.events declares the element form with the event stream (its log map kept)', D.form === 'element' && D.draw && D.draw.tag === 'vera-live-event-stream' && D.map && D.map.events === '$');
const WE = R('vera/widgets/widget_element.js');
t('the element form hands an element the answer it was opened from, when it can take it', WE.includes("if (typeof x.seed === 'function' && rec && rec.data != null) { try { x.seed(rec.data); }"));
// the stream's seed, run on a stub
const S = R('vera/workers/live_event_stream_element.js'); const i = S.indexOf('    seed(list) {'), j = S.indexOf('    _renderRow(ev, prepend) {', i);
const ctx = { MAX: 500, String, Array, Object }; vm.createContext(ctx);
vm.runInContext('var stub = { got: [], ingest(ev, at) { this.got.push([ev.name, at]); }, ' + S.slice(i, j).trim().replace(/^seed\(list\)/, 'seed: function (list)') + ' };', ctx);
ctx.stub.seed([{ name: 'c', ts: '2026-10-19T01:00:03Z' }, { name: 'a', ts: '2026-10-19T01:00:01Z' }, null, { name: 'b', ts: '2026-10-19T01:00:02Z' }, { name: 'n' }]);
t('seed feeds the events oldest first, each at its own time; one with no time is shown at arrival', JSON.stringify(ctx.stub.got) === JSON.stringify([['n', ''], ['a', '01:00:01'], ['b', '01:00:02'], ['c', '01:00:03']]));
t('a live event still shows its arrival time (ingest takes an optional time)', S.includes("ev._ts = at || new Date().toISOString().slice(11, 19);"));
console.log(bad ? bad + ' FAILED' : 'all passed'); process.exit(bad ? 1 : 0);
