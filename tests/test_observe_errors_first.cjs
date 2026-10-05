// The Estate's Health pane (was Observe; the pane id stays 'observe').
// 2026-09-28 (owner): "the widgets in observe panel are not focused on errors and they must be."
// 2026-10-02 (owner): "the observe panel widgets are not specific enough to vera health monitoring - i suppose it should be
//   called health and not observe".
// 2026-10-03 (owner): "the health page still has tonnes of widgets that are nothing to do with health - please remove them -
//   they are just duplicates of widgets that are all over vera in other places and the layout of the health page is still shit".
//   node tests/test_observe_errors_first.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const L = JSON.parse(R('vera/widgets/layouts/wol-observe.json'));
const WE = R('vera/widgets/widget_element.js'), PANEL = R('vera/workers/workers_ollama_panel.html'), VD = R('vera/chat/vera-dashboard.js');
const LES = R('vera/workers/live_event_stream_element.js'), SLE = R('vera/workers/system_log_element.js');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const id = (k) => L.widgets.find((w) => w.record.id === k);
const shown = L.widgets.filter((w) => !w.hidden), isSec = (w) => w.record.form === 'section';
const HEALTH = ['h-findings', 'h-stores', 'h-checks', 'h-stalls', 'h-redis', 'h-control', 'obs-errors', 'h-pending', 'h-stuck'];
const WHY = ['obs-error-log', 'obs-errors-by-cap', 'obs-error-trend'], HOST = ['h-host', 'h-proc-ram'];

// only health
t('the page shows only what says whether Vera is healthy and why not', shown.filter((w) => !isSec(w)).map((w) => w.record.id).sort().join() === HEALTH.concat(WHY, HOST).sort().join(), shown.map((w) => w.record.id).join(' '));
t('the duplicates of other panes are gone (job stream, scheduler, workers, runners, model traffic, census, GPU gate, recent errors)', ['obs-job-stream', 'h-scheduler', 'h-workers', 'h-runners', 'obs-inflight', 'obs-llm-by-worker', 'obs-llm-rate', 'obs-vera-work', 'obs-llm-routing', 'h-census', 'obs-gpu-gate', 'obs-recent-errors', 'obs-warnings', 'sec-h-logs', 'sec-h-traffic'].every((k) => !id(k)));
t('the live event stream and the system log, which the page draws itself, are hidden - not deleted', id('obs-stream') && id('obs-stream').hidden === true && id('obs-syslog') && id('obs-syslog').hidden === true && /data-wid="obs-stream"/.test(PANEL) && /data-wid="obs-syslog"/.test(PANEL));
t('the toolbar lost the stream\'s Clear and Pause', !/onclick="P\.obsClear\(\)" data-obs-events>Clear</.test(PANEL) && !/id="obs-pause-btn"/.test(PANEL) && /onclick="P\.gridReset\('observe'\)" data-obs-events>Reset</.test(PANEL));

// three bands, in order (VeraDash packs an unsaved dashboard tallest-first WITHIN a section: the banners hold the order)
const secs = shown.filter(isSec).map((w) => w.record.id);
t('three banners: is Vera healthy, why, Vera\'s host', secs.join() === 'sec-h-vera,sec-h-errors,sec-h-host' && shown[0].record.id === 'sec-h-vera');
const band = (k) => { const y = id(k).at[1]; return y < id('sec-h-errors').at[1] ? 0 : y < id('sec-h-host').at[1] ? 1 : 2; };
t('each tile sits in its band', HEALTH.every((k) => band(k) === 0) && WHY.every((k) => band(k) === 1) && HOST.every((k) => band(k) === 2));
t('the health tiles read Vera\'s own state', id('h-findings').record.source === 'perf.scan' && id('h-checks').record.source === 'perf.scan' && id('h-stores').record.source === 'obs.health' && id('h-control').record.source === 'obs.health' && id('h-stalls').record.source === 'perf.stalls' && id('h-redis').record.source === 'obs.redis' && id('h-stuck').record.source === 'nodes.runner.list' && id('h-pending').record.source === 'obs.pending' && WHY.concat(['obs-errors']).every((k) => id(k).record.source === 'syslog.error_summary'));
// the layout is laid at the heights VeraDash fits each form to, so the packed page has no holes
const cells = {}; let clash = 0, maxY = 0; shown.forEach((w) => { for (let y = w.at[1]; y < w.at[1] + w.span[1]; y++) for (let x = w.at[0]; x < w.at[0] + w.span[0]; x++) { if (cells[x + ',' + y]) clash++; cells[x + ',' + y] = 1; } maxY = Math.max(maxY, w.at[1] + w.span[1]); });
let holes = 0; for (let y = 0; y < maxY; y++) for (let x = 0; x < 12; x++) if (!cells[x + ',' + y]) holes++;
t('no tile overlaps another and no row has a hole', clash === 0 && holes === 0, clash + ' / ' + holes);
/* measured on prod 2026-10-03 (the w-hN VeraDash gave each tile): lists, the status, the stalls and the kv fit to 3 rows, figures to 2 - rows of equal height, widest first, are what its tallest-first packing lays without holes */
t('heights are the fitted ones: lists, the status, the stalls and the kv 3, figures 2, the log 7, charts 4', ['h-findings', 'h-stores', 'h-stalls', 'h-redis'].every((k) => id(k).span[1] === 3) && ['h-checks', 'h-control', 'obs-errors', 'h-pending', 'h-stuck'].every((k) => id(k).span[1] === 2) && id('obs-error-log').span[1] === 7 && ['obs-errors-by-cap', 'obs-error-trend', 'h-host', 'h-proc-ram'].every((k) => id(k).span[1] === 4));
t('no figure is narrower than four columns (two counters at 3x2 clipped their figures)', ['obs-errors', 'h-pending', 'h-stuck', 'h-checks', 'h-control'].every((k) => id(k).span[0] >= 4));
t('errors and warnings are one figure tile', id('obs-errors').record.form === 'numbers' && JSON.stringify(id('obs-errors').record.read.map.pick) === JSON.stringify({ errors: 'errors', warnings: 'warnings', critical: 'critical' }));
t('the error log spans the page', id('obs-error-log').span[0] === 12 && id('obs-error-log').record.form === 'log');

// the fixes these tiles depend on
t('the Redis tile asks obs.redis for the server figures alone (no 8 s keyspace scan)', JSON.stringify(id('h-redis').record.read.args) === '{"keys":false}' && /async def obs_redis\(keys: bool = True, trace_id=None\):/.test(R('vera/capability_orchestration.py')));
t('stuck runners is a figure, not the long note its source carries', id('h-stuck').record.form === 'numbers' && id('h-stuck').record.read.map.pick['stuck runners'] === 'stuck_count');
t('a status is fitted to its verdict and every check', /if \(f === 'status'\) return 'status';/.test(VD) && /else if \(cat === 'status'\) \{ if \(n == null\) return 0; content = 56 \+ Math\.min\(n, 12\) \* 20; \}/.test(VD));
t('the status sample is neutral - checking, every check grey', /status: \(\) => \(\{ status: 'checking', message: 'waiting for the first reading'/.test(WE));
t('the pane is named Health where it is shown; its id stays observe', /data-pane="observe" data-view="estate" title="Health - Vera's own health" onclick="P\.show\('observe',this\)"><span class="gl">◎<\/span>Health<\/butt/.test(PANEL) && /<span class="pane-tb-title" style="color:var\(--acc4\)">HEALTH<\/span>/.test(PANEL) && !/>Observe<\/butt/.test(PANEL));

/* (2026-10-03, prod) VeraDash injected the widget element ~2 s in, after the panes' start-up reads held all six connections: on a slow prod it queued 28-50 s and every tile stayed empty (with it in the head: defined at 0.5 s, all tiles live by 20 s) */
t('the panel asks for the widget element at parse time, marked so VeraDash does not add it again', /<script src="\/ui\/vera-dashboard\.js"><\/script>\s*<!--[\s\S]*?-->\s*<script src="\/ui\/widgets\/widget_element\.js" defer data-vera-widget-el="1"><\/script>/.test(PANEL) && /document\.querySelector\('script\[data-vera-widget-el\]'\)/.test(VD));
// the elements (hidden here, still used elsewhere and by the picker)
t('the stream and log elements keep their fixes', /\.row\{padding:3px 7px;border-radius:3px;font-size:12px;/.test(LES) && /disconnectedCallback\(\) \{\s*this\._gone = true;/.test(LES) && /\.entry-msg\{font-size:12px;/.test(SLE));

// the error tiles draw the answer, not their sample
const defined = {}; const ctx = { setTimeout, clearTimeout, window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const now = Date.now(), ans = { errors: 7, warnings: 12, critical: 1, total: 19, window_s: 3600,
  by_cap: [{ name: 'llm.generate', errors: 4, warnings: 2, count: 6 }], series: [0, 1, 2, 3].map((i) => ({ t: new Date(now - (3 - i) * 300000).toISOString(), errors: i, warnings: 0 })),
  entries: [{ ts: new Date(now - 60000).toISOString(), level: 'ERROR', cap_name: 'llm.generate', message: 'timeout after 900s' }],
  recent_errors: [], last_error: null };
const drawn = ['obs-errors'].concat(WHY).map((k) => { const n = W.normalise(id(k).record); return [k, W.draw(n.form, ans, 'l', { record: n, draw: n.draw, height: 200, width: 500 })]; });
t('each error tile draws the answer, not its sample', drawn.every(([, h]) => !/data-sample="1"/.test(h) && !/needs/.test(h)), drawn.filter(([, h]) => /data-sample="1"|needs/.test(h)).map(([k]) => k).join(' '));
t('the errors log shows the message', /timeout after 900s/.test(drawn.find(([k]) => k === 'obs-error-log')[1]));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
