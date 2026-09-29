// The multi-line chart and the keys (owner, 2026-09-28: "more multi-line charts that are color coded" · "better color coding
// and keys on all widgets that would make sense on"). Drawn from the sources' real answers (the design mirror, trimmed).
//   node tests/test_widget_lines_keys.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const items = (h) => [...h.matchAll(/data-item="([^"]*)"/g)].map((m) => JSON.parse(m[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')));
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const keyOf = (h) => { const m = h.match(/<div class="vb-lnk">([\s\S]*?)<\/div>/); return m ? m[1] : ''; };
const strokes = (h) => [...h.matchAll(/<polyline class="ln"[^>]*stroke="([^"]+)"/g)].map((m) => m[1]);

// ── the sources' answers (taken from the design mirror 2026-09-28, trimmed) ──
const SYSMON = { samples: [{ t: 1790585892.5, cpu: 100.0, mem: 82.3, proc_mb: 1382.0, temp_max: null }, { t: 1790585898.0, cpu: 59.7, mem: 82.4, proc_mb: 1397.6, temp_max: 78.0 }, { t: 1790585907.3, cpu: 63.9, mem: 82.2, proc_mb: 1403.7, temp_max: 78.0 }, { t: 1790585917.3, cpu: 34.2, mem: 82.7, proc_mb: 1583.9, temp_max: 78.0 }, { t: 1790585926.9, cpu: 32.1, mem: 82.6, proc_mb: 1588.0, temp_max: 78.0 }, { t: 1790585936.6, cpu: 40.7, mem: 82.6, proc_mb: 1594.2, temp_max: 78.0 }] };
const BENCH = { series: { 'cpu-246': [{ t: 1790585891.470155, tps: 1.2, ping_ms: 32.9, reachable: 1 }, { t: 1790585901.0741386, tps: 1.2, ping_ms: 9.8, reachable: 1 }, { t: 1790585916.115628, tps: 1.2, ping_ms: 22.7, reachable: 1 }, { t: 1790585931.1658518, tps: 1.2, ping_ms: 14.8, reachable: 1 }],
  'cpu-247': [{ t: 1790585891.470155, tps: 3.5, ping_ms: 32.5, reachable: 1 }, { t: 1790585901.0741386, tps: 3.5, ping_ms: 9.3, reachable: 1 }, { t: 1790585916.115628, tps: 3.5, ping_ms: 22.2, reachable: 1 }, { t: 1790585931.1658518, tps: 3.5, ping_ms: 13.4, reachable: 1 }],
  'gpu-250': [{ t: 1790585891.470155, tps: 36.5, ping_ms: 33.1, reachable: 1 }, { t: 1790585901.0741386, tps: 36.5, ping_ms: 10.5, reachable: 1 }, { t: 1790585916.115628, tps: 36.5, ping_ms: 23.3, reachable: 1 }, { t: 1790585931.1658518, tps: 36.5, ping_ms: 48.6, reachable: 1 }] }, interval_s: 15, count: 3 };
const ACT = { buckets: [{ hour: '2026-09-25T09', pass: 3, fail: 1, edits: 0 }, { hour: '2026-09-25T10', pass: 9, fail: 0, edits: 0 }, { hour: '2026-09-25T11', pass: 1, fail: 1, edits: 0 }, { hour: '2026-09-25T12', pass: 5, fail: 1, edits: 0 }, { hour: '2026-09-25T13', pass: 2, fail: 0, edits: 0 }, { hour: '2026-09-25T14', pass: 8, fail: 1, edits: 0 }, { hour: '2026-09-25T15', pass: 2, fail: 1, edits: 0 }, { hour: '2026-09-25T16', pass: 1, fail: 1, edits: 0 }], hours: 73 };
const CALLS = { source: 'cap_calls', count: 8, sample: [{ name: 'sandbox.session.commit', ts: '2026-09-28T08:59:51.458861Z', elapsed_ms: 39, via: '' }, { name: 'obs.redis', ts: '2026-09-28T08:59:51.441225Z', elapsed_ms: 2862, via: 'sandbox-read' }, { name: 'activity.timeline', ts: '2026-09-28T08:59:51.412748Z', elapsed_ms: 193, via: 'sandbox-read' }, { name: 'evolve.activity', ts: '2026-09-28T08:59:51.39032Z', elapsed_ms: 135, via: 'sandbox-read' }, { name: 'evolve.editq.list', ts: '2026-09-28T08:59:51.386165Z', elapsed_ms: 40, via: 'sandbox-read' }, { name: 'sandbox.session.context', ts: '2026-09-28T08:59:51.373204Z', elapsed_ms: 13, via: '' }, { name: 'sandbox.session.fs.write', ts: '2026-09-28T08:59:51.36693Z', elapsed_ms: 5, via: '' }, { name: 'sandbox.session.stop', ts: '2026-09-28T08:59:51.34792Z', elapsed_ms: 232, via: '' }] };

// ── the main overview's tiles now drawn as lines ──
const main = JSON.parse(fs.readFileSync(path.join(R, 'vera', 'widgets', 'layouts', 'main.json'), 'utf8'));
const tile = {}; main.widgets.forEach((w) => { if (w.record && w.record.id) tile[w.record.id] = w; });
const drawTile = (id, data, size, width, height) => { const w = tile[id], r = w.record; return W.draw(r.form, data, size || r.frame.size, { bare: true, sample: false, record: r, draw: r.draw, width: width || w.span[0] * 125, height: height || 150 }); };
t('host-cpu is cpu + memory on one chart; host-memory is hidden (it only repeated the second line)', tile['host-cpu'].record.form === 'lines' && JSON.stringify(tile['host-cpu'].record.read.map) === JSON.stringify({ series: 'samples', split: ['cpu', 'mem'], t: 't' }) && tile['host-memory'].hidden === true);
t('node latency, node throughput, test runs and call time are lines', ['node-perf', 'node-tps', 'lab-runs', 'cap-latency'].every((id) => tile[id].record.form === 'lines') && tile['cap-latency'].record.draw.by === 'via');

const cpu = drawTile('host-cpu', SYSMON);
t('host-cpu: two series, each its own colour, named in the key with its latest value', strokes(cpu).length === 2 && strokes(cpu)[0] !== strokes(cpu)[1] && /cpu[\s\S]*40\.7%[\s\S]*mem[\s\S]*82\.6%/.test(text(keyOf(cpu))), text(keyOf(cpu)));
t('host-cpu: a percent scale 0 – 100 with its gridlines and their values', (cpu.match(/class="gl"/g) || []).length >= 2 && (cpu.match(/class="gl"/g) || []).length <= 4 && /<div class="vb-lny"[^>]*>[\s\S]*>0%<[\s\S]*>100%</.test(cpu));
t('host-cpu: a time axis (the samples carry epoch seconds)', /class="vb-lnx"/.test(cpu) && /\d{2}:\d{2}/.test(cpu.match(/class="vb-lnx"[\s\S]*$/)[0]));
const cols = items(cpu).filter((x) => x.time); const ser = items(cpu).filter((x) => x.points);
t('host-cpu: a hover column carries every series\' value at that moment (the drawer opens on it)', cols.length >= 5 && cols.every((x) => typeof x.cpu === 'number' && typeof x.mem === 'number') && cols.some((x) => x.cpu === 100 && x.mem === 82.3), JSON.stringify(cols[0]));
t('host-cpu: each series is an item (name · latest · min · max · points) on its line and its key entry', ser.filter((x) => x.name === 'cpu').length === 3 && ser.find((x) => x.name === 'cpu').last === 40.7 && ser.find((x) => x.name === 'cpu').max === 100 && ser.find((x) => x.name === 'mem').min === 82.2 && /data-b="ln0"[\s\S]*data-b="ln0"/.test(cpu));
t('the hover column\'s card head is its time, then every series', /<g class="hx"[^>]*data-tip="[^"]*\d{2}:\d{2}[^"]*&#10;|<g class="hx"[^>]*data-tip="[^"\n]*\d{2}:\d{2}[^"]*\ncpu · [\d.]+%\nmem · [\d.]+%"/.test(cpu), (cpu.match(/<g class="hx"[^>]*data-tip="([^"]*)"/) || [])[1]);

const perf = drawTile('node-perf', BENCH), tps = drawTile('node-tps', BENCH);
t('node-perf: one line per node, the key names each with its latest ping', strokes(perf).length === 3 && new Set(strokes(perf)).size === 3 && /cpu-246[\s\S]*14\.8 ms[\s\S]*cpu-247[\s\S]*13\.4 ms[\s\S]*gpu-250[\s\S]*48\.6 ms/.test(text(keyOf(perf))), text(keyOf(perf)));
t('node-tps: the throughput field of the same answer, with its unit', strokes(tps).length === 3 && /gpu-250[\s\S]*36\.5 tok\/s/.test(text(keyOf(tps))), text(keyOf(tps)));
const runs = drawTile('lab-runs', ACT, 'xl', 1125, 170);
t('lab-runs: pass and fail per hour, pass in the good colour and fail in the bad (palette status)', strokes(runs).length === 2 && strokes(runs)[0] === 'var(--b-ac2)' && strokes(runs)[1] === 'var(--b-ac4)' && /pass[\s\S]*fail/.test(text(keyOf(runs))));
t('lab-runs: the hour buckets are times (2026-09-25T09 reads as an hour)', /class="vb-lnx"/.test(runs) && items(runs).filter((x) => x.time).length === 8);
t('lab-runs: at XL the key says each series\' peak too', /peak 9/.test(text(keyOf(runs))));
const lat = drawTile('cap-latency', CALLS);
t('cap-latency: the calls split by how they were made (direct · sandbox-read), each a line', strokes(lat).length === 2 && /direct[\s\S]*sandbox-read/.test(text(keyOf(lat))) && /ms/.test(text(keyOf(lat))), text(keyOf(lat)));

// ── the shapes it reads ──
const rowsDraw = W.draw('lines', [{ t: 1, a: 3, b: 5 }, { t: 2, a: 4, b: 2 }, { t: 3, a: 6, b: 1 }], 'm', { sample: false, draw: { series: ['a', 'b'] } });
t('rows with draw.series: one line per named field (no time axis for a bare index)', strokes(rowsDraw).length === 2 && !/vb-lnx/.test(rowsDraw) && /a[\s\S]*6[\s\S]*b[\s\S]*1/.test(text(keyOf(rowsDraw))));
const named = W.draw('lines', [{ name: 'ct126', points: [1, 2, 3] }, { name: 'ct121', points: [{ v: 3 }, { v: 1 }] }], 'm', { sample: false });
t('[{name, points}]: one line each, the shorter one ending at the right edge', strokes(named).length === 2 && /ct126[\s\S]*ct121/.test(text(keyOf(named))) && /points="1000\.0,[\d.]+" /.test(named.replace(/points="([^"]*) ([\d.]+,[\d.]+)"/g, 'points="$2" ')));
t('a plain list of numbers is one line, no key', strokes(W.draw('lines', [1, 3, 2, 5], 'm', { sample: false })).length === 1 && !/vb-lnk/.test(W.draw('lines', [1, 3, 2, 5], 'm', { sample: false })));
const stNames = W.draw('lines', { ok: [1, 2, 3], down: [0, 1, 0] }, 'm', { sample: false });
t('series named by status words take the status colours without a palette', strokes(stNames).join() === 'var(--b-ac2),var(--b-ac4)');
t('draw.colors names a series\' colour', strokes(W.draw('lines', { a: [1, 2], b: [2, 1] }, 'm', { sample: false, draw: { colors: { b: '#ff0000' } } }))[1] === '#ff0000');
t('frame.motion false draws it still', /vb-lines still/.test(W.draw('lines', { a: [1, 2], b: [2, 1] }, 'm', { sample: false, record: { frame: { motion: false } } })) && !/vb-lines still/.test(W.draw('lines', { a: [1, 2], b: [2, 1] }, 'm', { sample: false })));
t('a trace handed several series draws them as lines', /vb-lines/.test(W.draw('trace', { a: [1, 2, 3], b: [3, 2, 1] }, 'm', { sample: false })) && !/vb-lines/.test(W.draw('trace', [1, 2, 3], 'm', { sample: false })));
t('empty: its own empty line, not a sample', /wempty/.test(W.draw('lines', { a: [1] }, 'm', { sample: false })));

// ── registered: drawn, sampled, offered as a view, sized ──
t('lines is a drawn series form with a sample face, and the catalogue lists it', W.forms().some((f) => f.id === 'lines' && f.shape === 'series' && f.drawn) && /vb-lines/.test(W.draw('lines', null, 'm')) && /data-sample/.test(W.draw('lines', null, 'm'))
  && /_F\("lines", "series"/.test(fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_record.py'), 'utf8')));
t('view as: the series family offers lines', /series: \['trace', 'lines', 'area'/.test(WE));
t('XS and S say the first series\' latest', /cpu 40\.7/.test(W.draw('lines', { cpu: [1, 40.7], mem: [2, 82.6] }, 'xs')) && /cpu 40\.7 · mem 82\.6/.test(W.draw('lines', { cpu: [1, 40.7], mem: [2, 82.6] }, 's')));
t('at L the form keeps the whole body (its key names every value; no detail list beside it)', !/vw-detail/.test(W.draw('lines', { cpu: [1, 40.7], mem: [2, 82.6] }, 'l', { sample: false })));
const css = W.css();
t('its text follows the text-size setting (no fixed font under 13 px)', /\.vb-lnk\{[^}]*font-size:calc\(max\(var\(--vw-fmin/.test(css) && /\.vb-lny\{[^}]*font-size:calc\(max\(var\(--vw-fmin/.test(css) && /\.vb-lnx\{[^}]*font-size:calc\(max\(var\(--vw-fmin/.test(css));
t('a wider body keeps the key on fewer rows; a narrow one counts what does not fit', /class="more"/.test(W.draw('lines', Object.fromEntries(Array.from({ length: 8 }, (_, i) => ['series-number-' + i, [1, 2, i]])), 'm', { sample: false, width: 200 })));

// ── keys and colour on the other forms ──
const sm = W.draw('small-multiples', BENCH.series, 'm', { sample: false, record: { read: { map: { v: 'ping_ms' } } }, map: { series: 'series', v: 'ping_ms' } });
const smCols = [...sm.matchAll(/class="vb-smk" style="background:([^"]+)"/g)].map((m) => m[1]);
t('small multiples: each series its own colour and dot, each row its series item', smCols.length === 3 && new Set(smCols).size === 3 && items(sm).filter((x) => x.points).length === 3);
const ar = W.draw('area', { pass: [3, 9, 1], fail: [1, 0, 1] }, 'm', { sample: false });
t('area: a status-named series takes its status colour; the key says each latest and carries the series', /fill="var\(--b-ac2\)"/.test(ar) && /fill="var\(--b-ac4\)"/.test(ar) && /pass<b>1<\/b>/.test(ar) && items(ar).some((x) => x.name === 'fail' && x.points === 3));
const bs = W.draw('bars', { ok: 4, down: 1 }, 'm', { sample: false });
t('bars: bars named by a status take its colour and carry their item; others keep the accent', /fill="var\(--b-ac2,#5ec9a0\)"/.test(bs) && /fill="var\(--b-ac4,#e06060\)"/.test(bs) && items(bs).some((x) => x.name === 'down') && /fill="var\(--acc,#5a9e8f\)"/.test(W.draw('bars', { a: 1, b: 2 }, 'm', { sample: false })));
const rk = W.draw('ranked', [{ name: 'ct126', value: 62, status: 'ok' }, { name: 'ct130', value: 12, status: 'down' }, { name: 'ct121', value: 30, status: 'ok' }], 'm', { sample: false, height: 96 });
t('ranked: a row\'s status colours its bar, and a key of the statuses (with counts) sits under them', /background:var\(--b-ac4\)/.test(rk) && /vb-stkey[\s\S]*ok<b>2<\/b>[\s\S]*down<b>1<\/b>/.test(rk));
const sb = W.draw('stacked-bar', { running: 5, stopped: 2 }, 'm', { sample: false });
t('stacked bar: a segment and its key entry are one block with its share; status names colour by status', (sb.match(/data-b="sb0"/g) || []).length === 2 && items(sb).some((x) => x.name === 'stopped' && x.share === 28.6) && /background:var\(--b-ac4\)/.test(sb));
const tm = W.draw('treemap', { llm: 34, memory: 20, code: 14 }, 'm', { sample: false });
t('treemap: each cell carries its part and share', items(tm).some((x) => x.name === 'llm' && x.share === 50));
const pl = W.draw('pills', [{ name: 'redis', status: 'ok' }, { name: 'neo4j', status: 'ok' }, { name: 'ct130', status: 'down' }], 'm', { sample: false, height: 96 });
t('pills: a key of the statuses and how many of each', /vb-stkey[\s\S]*ok<b>2<\/b>[\s\S]*down<b>1<\/b>/.test(pl) && !/vb-stkey/.test(W.draw('pills', [{ name: 'a', status: 'ok' }, { name: 'b', status: 'ok' }], 'm', { sample: false })));
const dn = W.draw('donut', { running: 3, stopped: 1 }, 'm', { sample: false });
t('donut: status-named parts take the status colours unasked; other parts the theme\'s categorical set', /stroke="var\(--b-ac2\)"/.test(dn) && /stroke="var\(--b-ac4\)"/.test(dn) && /stroke="var\(--b-dv1\)"/.test(W.draw('donut', { llm: 3, embed: 1 }, 'm', { sample: false })));
const ht = W.draw('heat', [{ name: 'w1', cpu: 30, ram: 50 }, { name: 'w2', cpu: 80, ram: 20 }], 'm', { sample: false, height: 96 });
t('heat: a scale key under the grid (0 → the peak), the bands when the palette is load', /vb-heatkey[\s\S]*>0<[\s\S]*>80</.test(ht) && /vb-heatkey[\s\S]*&lt; 60[\s\S]*60–85[\s\S]*≥ 85/.test(W.draw('heat', [{ name: 'w1', cpu: 30, ram: 50 }], 'm', { sample: false, height: 96, draw: { palette: 'load' } })));

console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
