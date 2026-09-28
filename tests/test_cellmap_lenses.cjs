// 2026-09-28 (owner): "the cpu core stats indicator matrix ... with a variety of data lenses you can apply to the matrix, load, temp" ·
// "numa nodes and cpu pinning could be represented in the system widgets"
//   node tests/test_cellmap_lenses.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js'), M = JSON.parse(R('vera/widgets/layouts/main.json'));
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
// obs.node_temps as it answers (trimmed)
const temps = { hosts: [{ host_id: 'a', label: 'corp', pve: true, temps: { 'Package id 0': 76, 'Core 0': 76, 'Core 1': 88, 'Core 2': 71, temp1: 60 }, percpu: { cpu0: 12, cpu1: 95, cpu2: 64, cpu3: 3 } }, { host_id: 'b', label: 'empty', temps: {}, percpu: {} }] };
const load = W.draw('cellmap', temps, 'l', { bare: true, height: 200, width: 400, draw: { lens: 'load' } });
t('a square per thread, grouped by host, the hosts without readings left out', (load.match(/data-path="cell"/g) || []).length === 4 && />corp</.test(load) && !/>empty</.test(load), load.slice(0, 300));
t('the lenses the answer can fill are offered (load and core temperature), the chosen one lit', /data-vb-set="lens:load"/.test(load) && /data-vb-set="lens:temp"/.test(load) && /class="vb-cml on" data-vb-set="lens:load"/.test(load) && !/lens:pins/.test(load));
const temp = W.draw('cellmap', temps, 'l', { bare: true, height: 200, width: 400, ui: { lens: 'temp' } });
t('the temperature lens: the "Core N" sensors only (not the package or temp1), in °C', (temp.match(/data-path="cell"/g) || []).length === 3 && /°C/.test(temp), temp.slice(0, 300));
// pxstore.cpu.map as prod answers (trimmed)
const map = { topology: { cpus: [0, 1, 2, 3, 4, 5], numa_nodes: { 0: [0, 1, 2], 1: [3, 4, 5] } }, guests: [{ vmid: 1, name: 'a', status: 'running', cpus: [0, 1], flags: [] }, { vmid: 2, name: 'b', status: 'running', cpus: [1], flags: [] }, { vmid: 3, name: 'c', status: 'running', cpus: [2, 3], flags: ['spans-numa'] }, { vmid: 4, name: 'd', status: 'running', cpus: [], flags: ['unpinned'] }] };
const pins = W.draw('cellmap', map, 'l', { bare: true, height: 200, width: 400 });
t('the cpu map: a group per NUMA node, a square per cpu, shared cpus marked, the unpinned said', />NUMA 0</.test(pins) && />NUMA 1</.test(pins) && (pins.match(/data-path="cell"/g) || []).length === 6 && /shared/.test(pins) && /1 running guest unpinned/.test(pins) && /data-vb-set="lens:numa"/.test(pins) && /data-vb-set="lens:span"/.test(pins));
t('the dashboard carries the cores and the pinning in the Estate band, and the element reads the cpu map on its own', ['cpu-cores', 'cpu-pinning'].every((id) => M.widgets.some((w) => w.record && w.record.id === id && !w.hidden && w.record.form === 'cellmap')) && W.readable('pxstore.cpu.map') === true && W.readable('pxstore.cpu.pin') === false);
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
