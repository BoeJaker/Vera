// Per-core and per-GPU load under each model node (design-adopt resume item 3): the status matrix draws estate.compute.load -
// a group per machine serving models, the Proxmox host whole, GPU util and VRAM lenses - and the Ollama pane and the Estate's
// workers pane each carry it, each opening the other.
//   node tests/test_compute_load_cellmap.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
// estate.compute.load as it answered on prod, 2026-10-01 (trimmed)
const host = { label: 'PVE01', pve_node: 'corp', ref: 'pve:corp' };
const v100 = { index: 0, name: 'Tesla V100-PCIE-12GB', total_mb: 12288, used_mb: 8200, free_mb: 3853, util_pct: 91, temp_c: 60 };
const cl = { nodes: [
  { id: 'gpu-250', label: 'GPU Node', ip: '192.168.0.250', machine: { label: 'Ollama', vmid: 126, ref: 'guest:126' }, host, cores: [{ cpu: 12, load: 31.7 }, { cpu: 13, load: 95 }], gpus: [v100], same_machine: ['gpu-250-cpu'] },
  { id: 'gpu-250-cpu', label: 'GPU Node (CPU)', ip: '192.168.0.250', machine: { label: 'Ollama', vmid: 126, ref: 'guest:126' }, host, cores: [{ cpu: 12, load: 31.7 }, { cpu: 13, load: 95 }], gpus: [v100], same_machine: ['gpu-250'] },
  { id: 'cpu-246', label: 'CPU Node A', ip: '192.168.0.246', machine: { label: 'Ollama-B', vmid: 129, ref: 'guest:129' }, host, cores: [{ cpu: 0, load: 14 }, { cpu: 1, load: 10.1 }, { cpu: 2, load: 70 }], gpus: [] },
], hosts: [{ ...host, cores: [0, 1, 2, 3].map((c) => ({ cpu: c, load: c * 30 })), nodes: ['gpu-250', 'cpu-246', 'gpu-250-cpu'] }] };
const load = W.draw('cellmap', cl, 'l', { bare: true, height: 240, width: 500, draw: { lens: 'load' } });
const cells = (h) => (h.match(/data-path="cell"/g) || []).length;
t('one group per machine (two instances on one CT share it), then the host whole', />GPU Node \+ GPU Node \(CPU\)</.test(load) && />CPU Node A</.test(load) && />PVE01 · whole host</.test(load) && cells(load) === 2 + 3 + 4, load.slice(0, 400));
t('a core says where it is: the CT and its host', /CT 126/.test(load) && /on PVE01/.test(load));
t('the load, GPU util and VRAM lenses are offered; temperature and pinning are not', ['load', 'gpu', 'vram'].every((l) => new RegExp('data-vb-set="lens:' + l + '"').test(load)) && !/lens:temp/.test(load) && !/lens:pins/.test(load));
const gpu = W.draw('cellmap', cl, 'l', { bare: true, height: 240, width: 500, ui: { lens: 'gpu' } });
t('the GPU lens: one square per card, only on machines with one', cells(gpu) === 1 && />GPU Node \+ GPU Node \(CPU\)</.test(gpu) && !/>CPU Node A</.test(gpu) && /peak 91%/.test(gpu), gpu.slice(0, 400));
const vram = W.draw('cellmap', cl, 'l', { bare: true, height: 240, width: 500, ui: { lens: 'vram' } });
t('the VRAM lens is used over total, as a percent', cells(vram) === 1 && /peak 67%/.test(vram), vram.slice(0, 400));
t('obs.node_temps tiles are unchanged: no GPU lenses without GPUs', !/lens:gpu/.test(W.draw('cellmap', { hosts: [{ label: 'corp', percpu: { cpu0: 1 }, temps: { 'Core 0': 50 } }] }, 'l', { bare: true, height: 200, width: 400 })));
t('a widget reads the join on its own', W.readable('estate.compute.load') === true);
// the two panes carry it, each opening the other
const tileIn = (f, id) => (JSON.parse(R('vera/widgets/layouts/' + f)).widgets || []).find((w) => w.record && w.record.id === id);
const ol = tileIn('wol-ollama.json', 'ol-compute-load'), wk = tileIn('wol-workers.json', 'w-compute-load');
t('Models > Ollama: under the node cards, opens the Estate workers pane', ol && !ol.hidden && ol.record.form === 'cellmap' && ol.record.source === 'estate.compute.load' && ol.record.open === 'estate/workers');
t('Estate > Workers: under the worker cards, opens Models', wk && !wk.hidden && wk.record.form === 'cellmap' && wk.record.source === 'estate.compute.load' && wk.record.open === 'models');
const overlaps = (f) => { const ws = JSON.parse(R('vera/widgets/layouts/' + f)).widgets.filter((w) => !w.hidden); for (let i = 0; i < ws.length; i++) for (let j = i + 1; j < ws.length; j++) { const a = ws[i], b = ws[j]; if (a.at[0] < b.at[0] + b.span[0] && b.at[0] < a.at[0] + a.span[0] && a.at[1] < b.at[1] + b.span[1] && b.at[1] < a.at[1] + a.span[1]) return a.record.id + ' x ' + b.record.id; } return ''; };
t('no tile overlaps another in either pane', !overlaps('wol-ollama.json') && !overlaps('wol-workers.json'), overlaps('wol-ollama.json') + overlaps('wol-workers.json'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
