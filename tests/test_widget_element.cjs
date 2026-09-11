// <vera-widget> / window.VeraWidget (UI redesign M2): every drawn form renders fixture data at M; XS and S are the
// compositions the Sizes board names; L adds the detail list and XL the table; an unknown form says so; both record
// shapes normalise to one; the shape rule and the fallback rule are the chat's, now here.
//   node tests/test_widget_element.cjs   (CommonJS: the pipeline's gate parses every js file as a script)
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const here = __dirname;
const src = fs.readFileSync(path.join(here, '..', 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {} }) } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document;
vm.runInNewContext(src, ctx);
const W = ctx.window.VeraWidget;
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

t('api', ['draw', 'forms', 'normalise', 'formByShape', 'dataFor', 'formFor', 'readable', 'key', 'hydrate', 'css', 'ensureCss'].every((k) => typeof W[k] === 'function') && W.version === 1);
t('the element is defined', !!defined['vera-widget']);
const series = [{ t: 1, v: 40 }, { t: 2, v: 48 }, { t: 3, v: 62 }, { t: 4, v: 55 }];
const fix = {
  trace: [series, /<polyline/], radial: [{ value: 62, min: 0, max: 100 }, /62%<\/text>/], counter: [{ value: 1234, unit: 'ms', delta: -12 }, /vw-hero[\s\S]*1,234[\s\S]*ms[\s\S]*down/],
  bar: [{ value: 7, min: 0, max: 10, unit: 'G' }, /vw-meter[\s\S]*width:70\.0%/], bars: [{ a: 3, b: 9, c: 1 }, /<rect[\s\S]*<rect[\s\S]*<rect/], thermo: [{ cpu: 62, gpu: 31 }, /vw-therm[\s\S]*cpu/],
  heat: [Object.fromEntries('abcdefghij'.split('').map((k, i) => [k, i + 1])), /vw-heat[\s\S]*grid-template-columns:repeat\(4,1fr\)/], matrix: [{ ct126: { api: 'ok', db: 'down' }, ct121: { api: 1, db: 0.5 } }, /vw-matrix[\s\S]*ct126/],
  donut: [{ running: 26, paused: 3, stopped: 4 }, /vw-donut[\s\S]*running/], stack: [{ llm: 5, embed: 3 }, /vw-stackbar/], pills: [[{ name: 'redis', status: 'ok' }, { name: 'neo4j', status: 'down' }], /vw-pill ok[\s\S]*vw-pill bad/],
  log: [[{ t: '2026-09-11T09:21:00', kind: 'INFO', text: 'pull skipped' }], /vw-log[\s\S]*09:21:00[\s\S]*pull skipped/], lane: [[{ t: 1, kind: 'a', text: 'x' }, { t: 2, kind: 'b', text: 'y' }], /lane">a · 1/],
  table: [[{ name: 'ct126', load: 0.6 }, { name: 'ct121', load: 0.2 }], /<th>name<\/th><th>load<\/th>/], files: [[{ path: '/a.py', size: 12 }], /<th>path<\/th>/], list: [['one', 'two'], /vw-list[\s\S]*one/],
  checklist: [[{ text: 'gate', done: true }, { text: 'sweep' }], /class="on"[\s\S]*☑[\s\S]*☐/], stepper: [{ stages: [{ name: 'plan', done: true }, { name: 'act', current: true }, { name: 'verify' }] }, /done[\s\S]*now[\s\S]*verify/],
  calendar: [[{ when: '2026-09-11T16:00', title: 'sweep' }], /vw-cal[\s\S]*2026-09-11[\s\S]*16:00/], string: ['hello', /vw-str">hello/], kv: [{ ok: true, node: 'ct126' }, /vw-kv[\s\S]*node/],
  scatter: [[{ x: 1, y: 2 }, { x: 3, y: 4 }], /<circle[\s\S]*<circle/], pipes: [{ nodes: [{ id: 'a' }, { id: 'b' }], links: [{ source: 'a', target: 'b' }] }, /vw-mm[\s\S]*data-mm-code="graph LR/],
};
let ok = true;
for (const [form, [data, re]] of Object.entries(fix)) { const h = W.draw(form, data, 'm'); if (!re.test(h) || /class="wempty"/.test(h)) { ok = false; console.log('  ' + form + ' → ' + h.slice(0, 160)); } }
t('every drawn form renders its fixture at M (' + Object.keys(fix).length + ' forms)', ok);
t('aliases resolve to a renderer', /<polyline/.test(W.draw('sparkline', series, 'm')) && /<polyline/.test(W.draw('line', series, 'm')) && /62%/.test(W.draw('ring', { value: 62, min: 0, max: 100 }, 'm')) && /<rect/.test(W.draw('ranked', { a: 1, b: 2 }, 'm')));
t('unknown form says so', /class="wempty">form zzz · no drawing yet/.test(W.draw('zzz', {}, 'm')));
t('no data says so', /no data yet/.test(W.draw('trace', null, 'm')) && /a trace needs two/.test(W.draw('trace', [1], 'm')));
t('XS is glyph + one figure', /^<span class="vw-xs"[\s\S]*<i>∿<\/i>55<\/span>$/.test(W.draw('trace', series, 'xs')));
t('S is a chip with the label', /^<span class="vw-chip"[\s\S]*<b>62%<\/b><small>gate<\/small><\/span>$/.test(W.draw('radial', { value: 62, min: 0, max: 100 }, 's', { title: 'gate' })));
t('L adds the detail list', /vw-l[\s\S]*vw-therm[\s\S]*vw-detail[\s\S]*cpu/.test(W.draw('thermo', { cpu: 62, gpu: 31 }, 'l')));
t('XL adds the table', /vw-xl[\s\S]*vw-detail[\s\S]*vw-xltable[\s\S]*<th>name<\/th><th>value<\/th>/.test(W.draw('thermo', { cpu: 62, gpu: 31 }, 'xl')));
t('bare returns just the form at any size', !/vw-l|vw-xl/.test(W.draw('thermo', { cpu: 62 }, 'xl', { bare: true })));
t('the envelope is seen through', /<polyline/.test(W.draw('trace', { ok: true, history: series }, 'm')));
// the shape rule (the Formats board's table) and the fallback
t('formByShape', W.formByShape({ value: 3, max: 10 }) === 'radial' && W.formByShape({ value: 3 }) === 'counter' && W.formByShape(series) === 'trace' && W.formByShape([{ x: 1, y: 2 }]) === 'scatter' && W.formByShape({ a: 1, b: 2 }) === 'thermo' && W.formByShape(Object.fromEntries('abcdefghij'.split('').map((k) => [k, 1]))) === 'heat' && W.formByShape([{ t: 1, kind: 'x', text: 'y' }]) === 'log' && W.formByShape({ nodes: [], links: [] }) === 'pipes' && W.formByShape([{ name: 'a' }]) === 'table' && W.formByShape([{ path: 'p' }]) === 'files' && W.formByShape([{ text: 'x', done: true }]) === 'checklist' && W.formByShape({ stages: [] }) === 'stepper' && W.formByShape({ ok: true, items: series }) === 'trace' && W.formByShape({ ok: true, text: 'hi' }) === null);
t('formFor: the chosen form, else the shape, else kv', W.formFor({ form: 'trace' }, series) === 'trace' && W.formFor({ form: 'table' }, { cpu: 62, gpu: 31 }) === 'thermo' && W.formFor({ form: 'table' }, { ok: true, node: 'ct126' }) === 'kv' && W.formFor({ form: 'radial' }, undefined) === 'radial');
// the record in one shape
const a = W.normalise({ name: 'GPU + queue', form: 'meter', reads: { cap: 'sysmon.status', args: { node: 'ct126' }, every: '10s' }, draw: { form: 'meter', size: 'S', glow: 1 } });
const b = W.normalise({ title: 'GPU', form: 'trace', source: 'sysmon.history', window: '1h', size: 'l', refresh: '5s' });
const c = W.normalise({ name: 'Gate', form: 'meter', reads: { cap: 'obs.pending' }, draw: { form: 'radial', size: 'S' } });
t('normalise: the template shape draws draw.form, not the kind', c.form === 'radial' && c.frame.size === 's');
t('normalise: the template shape', a.form === 'meter' && a.source === 'sysmon.status' && a.read.args.node === 'ct126' && a.read.refresh === '10s' && a.frame.size === 's' && a.draw.glow === 1 && a.title === 'GPU + queue');
t('normalise: the short form', b.form === 'trace' && b.source === 'sysmon.history' && b.read.window === '1h' && b.read.args.window === '1h' && b.frame.size === 'l' && b.read.refresh === '5s');
t('normalise: a panel record', W.normalise({ form: 'panel', source: 'panel:system-monitor' }).panel === 'system-monitor');
t('key is form · source · args', W.key({ form: 'trace', source: 'obs.events', args: { window: '1h' } }) === 'trace obs.events {"window":"1h"}');
t('readable: quiet reads only', W.readable('obs.health') && W.readable('sysmon.history') && W.readable('jobs.list') && !W.readable('code.write') && !W.readable('exec.code.run') && !W.readable('canvas.create'));
t('hydrate is a no-op without the mermaid element', W.hydrate({ querySelectorAll: () => [{}] }) === 0);
t('css names the classes the markup uses', /\.wempty/.test(W.css()) && /\.vw-therm/.test(W.css()) && /\.vw-xl/.test(W.css()));
t('forms() lists the drawn forms and the aliases', W.forms().some((f) => f.id === 'trace' && f.shape === 'series') && W.forms().some((f) => f.id === 'sparkline' && f.as === 'trace') && W.forms().length > 60);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
