// DO-0.3 (desk output redesign): the stale declarations draw what their data is, and the desk never shows sample data.
// Real answers read on the design mirror (:8998, 18 Oct) where it has them; the page's own adapter block, the real widget
// element, present.js and the canvas element's widget body.
//   node tests/test_desk_output_do03.cjs      (CommonJS: the gate parses js as scripts)
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.join(__dirname, '..');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const CHAT = fs.readFileSync(path.join(root, 'vera', 'chat', 'chat_panel.html'), 'utf8');
const WE = fs.readFileSync(path.join(root, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const DECL = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'presentation_declared.json'), 'utf8'));

function widgets() {
  const defined = {};
  const c = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, k) => { defined[n] = k; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
  c.window.customElements = c.customElements; c.window.document = c.document;
  vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'ui', 'iso.js'), 'utf8'), c);
  vm.runInNewContext(WE, c);
  vm.runInNewContext(fs.readFileSync(path.join(root, 'vera', 'widgets', 'present.js'), 'utf8'), c);
  return c.window.VeraWidget;
}
const W = widgets();
W.presentations(Object.entries(DECL).map(([name, p]) => ({ name, presentation: Object.assign({ status: 'declared' }, p) })));
const A = "  const CV_ADAPTERS=[];", B = "  function _cvHarvest(body){";
const i0 = CHAT.indexOf(A), i1 = CHAT.indexOf(B);
const ctx = { window: { VeraWidget: W }, VeraWidget: W, URL, ALL_CAPS: [], console, _widgetData: (x) => x, _widgetFormByShape: () => null, _widgetRecord: (o) => o };
vm.createContext(ctx); vm.runInContext(CHAT.slice(i0, i1) + '\n;this.__adapt=_cvAdapt;', ctx);
const adapt = ctx.__adapt;
const CV = require(path.join(root, 'vera', 'canvas', 'canvas_element.js'));

// ── entities: nlp.ner's answer (as :8998 gave it) is the desk's entities element, not a JSON tree ──
const NER = { ok: true, task: 'ner', model: 'djagatiya/ner-roberta-base-ontonotesv5-englishv4', entities: [{ entity: 'PERSON', word: ' Ada Lovelace', score: 0.998, start: 0, end: 12 }, { entity: 'PERSON', word: ' Charles Babbage', score: 0.998, start: 17, end: 32 }, { entity: 'GPE', word: ' London', score: 0.997, start: 36, end: 42 }, { entity: 'DATE', word: ' 1833', score: 0.99, start: 46, end: 50 }, { entity: 'PERSON', word: ' Ada Lovelace', score: 0.99, start: 60, end: 72 }], count: 5, node: 'cpu-247' };
t('nlp.ner declares the entities kind', DECL['nlp.ner'] && DECL['nlp.ner'].kind === 'entities');
const pe = W.present('nlp.ner', NER, { args: { text: 'x' } });
t('present(): the names one row each, trimmed, the same name of a type counted once more - a widget draws them as pills', pe.element === 'entities' && pe.records[0].form === 'pills'
  && JSON.stringify(pe.records[0].data[0]) === JSON.stringify({ name: 'Ada Lovelace', type: 'PERSON', count: 2, group: 'PERSON' }) && pe.records[0].data.length === 4, JSON.stringify(pe.records[0] && pe.records[0].data));
t('...and the pills draw the names, not an empty face', /Ada Lovelace/.test(W.draw('pills', pe.records[0].data, 'm', { sample: false })) && !/wempty/.test(W.draw('pills', pe.records[0].data, 'm', { sample: false })));
const ei = adapt('nlp.ner', NER, { text: 'x' });
t('the desk lands the entities element: its groups by type, each name with its count', ei && ei.kind === 'entities' && Array.isArray(ei.content.groups) && ei.content.groups.length === 3
  && ei.content.groups[0].group === 'PERSON' && ei.content.groups[0].entities[0].text === 'Ada Lovelace' && ei.content.groups[0].entities[0].count === 2, JSON.stringify(ei));
const eh = CV.BLOCK.entities(ei.content, 'm', 'k', null);
t('...which the canvas draws as chips (its list and graph views beside)', /data-w="canvas.entities"/.test(eh) && /vc-chip/.test(eh) && /Charles Babbage/.test(eh));
t('a groups answer (turn.understand\'s) reads to the same rows, its groups kept', JSON.stringify(W.present('x', { groups: [{ group: 'People', entities: [{ text: 'Ada', type: 'PERSON' }] }] }, { presentation: { kind: 'entities', status: 'declared', map: { groups: 'groups' } } }).records[0].data) === JSON.stringify([{ name: 'Ada', type: 'PERSON', count: 1, group: 'People' }]));
t('no names: no item, the declaration\'s empty line', W.present('nlp.ner', { ok: true, entities: [] }).empty === 'no names found' && adapt('nlp.ner', { ok: true, entities: [] }, {}) === null);

// ── markets.chart: a chart answer is the Markets chart on the desk; an answer with no bars is no blank chart ──
const CH = { symbol: 'BTC-USD', key: 'yahoo:BTC-USD', provider: 'yahoo', timeframe: '1d', last: 2.5, change_pct: 1.2, count: 3, bars: [{ t: 1, open: 1, high: 2, low: 0.5, close: 1.5, volume: 3 }, { t: 2, open: 1.5, high: 2.5, low: 1, close: 2, volume: 4 }, { t: 3, open: 2, high: 3, low: 1.5, close: 2.5, volume: 5 }] };
t('markets.chart: the series kind\'s candles, its views the chart\'s own (no trace / area / column of an OHLC answer)', DECL['markets.chart'].form === 'candles' && JSON.stringify(DECL['markets.chart'].views) === '["table"]');
const mi = adapt('markets.chart', CH, { symbol: 'btc' });
t('the desk item is a candles record carrying its symbol', mi && mi.kind === 'widget' && mi.content.form === 'candles' && mi.content.data && mi.content.data.symbol === 'BTC-USD', JSON.stringify(mi && mi.content).slice(0, 200));
global.customElements = { get: (n) => (n === 'vera-widget' ? function () {} : undefined) };
const mh = CV.BLOCK.widget(mi.content, 'l', 'widget:btc');
t('...which the canvas draws as <vera-mkt-chart> (the Markets panel\'s chart), at its symbol, provider and timeframe', /data-live="mchart"/.test(mh) && /data-symbol="BTC-USD"/.test(mh) && /data-provider="yahoo"/.test(mh) && /data-tf="1d"/.test(mh), mh.slice(0, 300));
const ERR = { error: 'AAPL is not tracked - add it to the watchlist to chart it', tracked: [] };   // :8998's answer
const pm = W.present('markets.chart', ERR, { args: { symbol: 'AAPL' } });
t('an answer with no bars: no record, its own error as the line (it drew an empty candle chart)', pm.records.length === 0 && pm.empty === ERR.error && pm.title === 'AAPL', JSON.stringify(pm));
t('...and no desk item (the tool card stays in the chat)', adapt('markets.chart', ERR, { symbol: 'AAPL' }) === null);

// ── docker.stats.top and evolve.sandbox.status: no longer raw JSON ──
const DS = { hosts: { local: { containers: [{ id: 'a', name: 'vera', cpu_pct: 41.5, mem_mb: 900, history: [] }], updated_at: 'x', error: '' }, b: { containers: [{ id: 'b', name: 'redis', cpu_pct: 3, mem_mb: 40 }] } },
  top: [{ host: 'local', id: 'a', name: 'vera', cpu_pct: 41.5, mem_mb: 900 }, { host: 'b', id: 'b', name: 'redis', cpu_pct: 3, mem_mb: 40 }] };
const di = adapt('docker.stats.top', DS, {});
t('docker.stats.top: the busiest containers ranked by CPU', di && di.kind === 'widget' && di.content.form === 'ranked' && /vera/.test(W.draw('ranked', di.content.data, 'm', { record: di.content, sample: false })), JSON.stringify(di).slice(0, 200));
t('docker.stats.top with no container sampled (:8998 has no docker socket): no item', adapt('docker.stats.top', { hosts: { local: { containers: [], error: 'HTTP 503: no local docker socket' } }, top: [] }, {}) === null);
const SB = { sandbox: {}, probe: { reachable: false }, up: false, paused: false, routing_drift: { ok: true, checked: false }, idle_pause_s: 1800, dev_port: 8998, prod_port: 8999, dev_redis_db: 3,
  verdict: 'none', checks: [{ name: 'sandbox registered', status: 'none' }, { name: 'dev port 8998 reachable', status: 'down' }, { name: 'routing matches prod', status: 'not checked' }] };
const si = adapt('evolve.sandbox.status', SB, {});
t('evolve.sandbox.status: a status card - the verdict and its three checks', si && si.kind === 'widget' && si.content.form === 'status' && /dev port 8998 reachable/.test(W.draw('status', si.content.data, 'l', { sample: false })), JSON.stringify(si).slice(0, 240));

// ── metric: its declared views reach ring and trace ──
const mr = W.present('demo.m', { count: 5, hist: [1, 2, 3, 5] }, { presentation: { kind: 'metric', status: 'declared', map: { value: 'count', trend: 'hist' } } }).records[0];
const mv = W.viewsFor(mr, mr.form, mr.data);
t('a metric with a trend: the view-as offers ring and trace (its declared views), and the trace draws the trend', mv.includes('ring') && mv.includes('trace') && /<polyline/.test(W.draw('trace', mr.data, 'm', { record: mr, sample: false })), JSON.stringify(mv));
const m0 = W.present('demo.m', { count: 5 }, { presentation: { kind: 'metric', status: 'declared', map: { value: 'count' } } }).records[0];
t('...a metric without one offers no trace', !W.viewsFor(m0, m0.form, m0.data).includes('trace') && W.viewsFor(m0, m0.form, m0.data).includes('ring'));

// ── no sample data on the desk ──
t('a declared form with nothing in its content: no record (every form, not only the row forms)', W.present('x', { nodes: [] }, { presentation: { kind: 'kv', status: 'declared', form: 'pills', map: { rows: 'nodes' } } }).records.length === 0
  && W.present('x', { error: 'down' }, { presentation: { kind: 'status', status: 'declared', form: 'status-matrix' } }).empty === 'down');
t('...an answer with content still draws', W.present('x', { nodes: [{ name: 'a', status: 'ok' }] }, { presentation: { kind: 'kv', status: 'declared', form: 'pills', map: { rows: 'nodes' } } }).records.length === 1);
t('the desk\'s live widget is no-sample, and its static fallback never samples', /inner\.setAttribute\('no-sample', ''\);/.test(fs.readFileSync(path.join(root, 'vera', 'canvas', 'canvas_element.js'), 'utf8'))
  && !/data-sample/.test(CV.BLOCK.widget({ form: 'gauge', title: 'x', data: {} }, 'm', '') || ''));
t('the element: no-sample draws reading / read failed / nothing to show, never the sample', /const noSample = this\.hasAttribute\('no-sample'\);/.test(WE) && /const sampled = blank && !noSample;/.test(WE)
  && /\? \(blank && noSample \? unreadFace\(state, rec\.source, size, this\._err\) : NODATA\(rec\.source, size\)\)/.test(WE) && /'no-sample'\]; \}/.test(WE));
t('a composite on a no-sample host: its children draw no sample either', WE.includes("sample: (wasRead || (o && o.sample === false)) ? false : undefined"));

console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
