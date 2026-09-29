// 2026-09-28 (owner): "the observe ui live event stream and the system log are over the top of other widgets. the Job stream
// section is hard to see can only see the top. the widgets in observe panel are not focused on errors and they must be."
//   node tests/test_observe_errors_first.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const L = JSON.parse(R('vera/widgets/layouts/wol-observe.json'));
const WE = R('vera/widgets/widget_element.js'), PANEL = R('vera/workers/workers_ollama_panel.html');
const LES = R('vera/workers/live_event_stream_element.js'), SLE = R('vera/workers/system_log_element.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const id = (k) => L.widgets.find((w) => w.record.id === k);

// errors lead
const first = L.widgets.filter((w) => w.at[1] < 8).map((w) => w.record.id);
t('the first rows are the warnings and errors', ['obs-errors', 'obs-warnings', 'obs-last-error', 'obs-error-trend', 'obs-error-log', 'obs-errors-by-cap'].every((k) => first.includes(k)) && first.length === 6, first.join(' '));
t('they all read syslog.error_summary', first.every((k) => id(k).record.source === 'syslog.error_summary'));
t('the errors log is the widest tile of the band', id('obs-error-log').span[0] === 8 && id('obs-error-log').record.form === 'log');
// nothing was dropped (never remove widgets)
t('every earlier tile is still there', ['obs-inflight', 'obs-gpu-gate', 'obs-llm-by-worker', 'obs-llm-rate', 'obs-vera-work', 'obs-llm-routing', 'obs-job-stream', 'obs-stream', 'obs-syslog'].every((k) => !!id(k)));
// the job stream has room
t('the job stream is an XL composite of six rows', id('obs-job-stream').span[1] >= 6 && id('obs-job-stream').record.frame.size === 'xl');
// the two feeds are element widgets filling their span
t('the live event stream is its element, drawn by the record', id('obs-stream').record.form === 'element' && id('obs-stream').record.draw.tag === 'vera-live-event-stream' && id('obs-stream').record.draw.body === 'record');
t('the system log is its element, drawn by the record', id('obs-syslog').record.form === 'element' && id('obs-syslog').record.draw.tag === 'vera-system-log' && id('obs-syslog').record.draw.body === 'record' && !id('obs-syslog').record.children);
t('both are ten rows tall', id('obs-stream').span[1] === 10 && id('obs-syslog').span[1] === 10);
t('the widget element knows both tags and their scripts', /'vera-live-event-stream': 'live_event_stream', 'vera-system-log': 'system_log'/.test(WE) && /'vera-live-event-stream': 'live event stream', 'vera-system-log': 'system log'/.test(WE));
t('the page no longer top-aligns them (they ran over the tiles around them)', !/#obs-grid>\.widget\[data-wid="obs-syslog"\]\{align-self:start\}/.test(PANEL));
t('a converted tile retires the page chrome outside its body (the stream\'s filter bar)', /#obs-grid>\.widget\[data-converted\] > :not\(\.w-head\):not\(\.w-body\):not\(\.w-resize\)\{display:none!important\}/.test(PANEL));
// no overlaps, no holes
const cells = {}; let clash = 0, maxY = 0; L.widgets.forEach((w) => { for (let y = w.at[1]; y < w.at[1] + w.span[1]; y++) for (let x = w.at[0]; x < w.at[0] + w.span[0]; x++) { if (cells[x + ',' + y]) clash++; cells[x + ',' + y] = 1; } maxY = Math.max(maxY, w.at[1] + w.span[1]); });
let holes = 0; for (let y = 0; y < maxY; y++) for (let x = 0; x < 12; x++) if (!cells[x + ',' + y]) holes++;
t('no tile overlaps another and no row has a hole', clash === 0 && holes === 0, clash + ' / ' + holes);
// the elements
t('the event stream reads larger (12 px rows)', /\.row\{padding:3px 7px;border-radius:3px;font-size:12px;/.test(LES));
t('a stream taken off a dashboard closes its socket for good', /disconnectedCallback\(\) \{\s*this\._gone = true;/.test(LES) && /this\._ws\.onclose = \(\) => \{ if \(!this\._gone\) setTimeout/.test(LES));
t('the system log reads larger (12 px messages)', /\.entry-msg\{font-size:12px;/.test(SLE));
t('the system log opens at a level an attribute names, and can follow', /const lv = String\(this\.getAttribute\('level'\) \|\| ''\)\.toUpperCase\(\);/.test(SLE) && /this\.hasAttribute\('auto'\)/.test(SLE) && /<option value="CRITICAL">CRITICAL<\/option>/.test(SLE));
// every new tile draws real data from a summary-shaped answer (not its sample face)
const defined = {}; const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const now = Date.now(), ans = { errors: 7, warnings: 12, critical: 1, total: 19, window_s: 3600,
  by_cap: [{ name: 'llm.generate', errors: 4, warnings: 2, count: 6 }], series: [0, 1, 2, 3].map((i) => ({ t: new Date(now - (3 - i) * 300000).toISOString(), errors: i, warnings: 0 })),
  entries: [{ ts: new Date(now - 60000).toISOString(), level: 'ERROR', cap_name: 'llm.generate', message: 'timeout after 900s' }],
  last_error: { ts: new Date(now - 60000).toISOString(), level: 'ERROR', cap_name: 'llm.generate', message: 'timeout after 900s' } };
const drawn = first.map((k) => { const n = W.normalise(id(k).record); const h = W.draw(n.form, ans, 'l', { record: n, draw: n.draw, height: 200, width: 500 }); return [k, h]; });
t('each errors tile draws the answer, not its sample', drawn.every(([, h]) => !/data-sample="1"/.test(h) && !/needs/.test(h)), drawn.filter(([, h]) => /data-sample="1"|needs/.test(h)).map(([k]) => k).join(' '));
t('the errors log shows the message', /timeout after 900s/.test(drawn.find(([k]) => k === 'obs-error-log')[1]));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
