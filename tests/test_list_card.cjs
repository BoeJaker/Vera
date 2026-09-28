// 2026-09-28 (owner): "i still think the chip tables need to be vastly improved" - the list card
//   node tests/test_list_card.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs'), vm = require('node:vm');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', ...p.split('/')), 'utf8');
const WE = R('vera/widgets/widget_element.js');
let fails = 0; const t = (name, cond, x) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (x || ''))); if (!cond) fails++; };
const defined = {}; const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null, addEventListener() {} }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(R('vera/ui/iso.js'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const sbx = [{ name: 'vera-dev', running: false, branch: 'loop-lab/sandbox', port: 8998 }, { name: 'mirror', running: true, branch: 'bleeding-edge-design', port: 8991 }, { name: 'feat-x', running: true, branch: 'feat/x', port: 8990 }];
const rec = { form: 'rows', read: { map: { rows: 'sandboxes', name: 'name', status: 'running', value: 'port' } } };
const h = W.draw('rows', sbx.map((r) => ({ name: r.name, status: r.running, value: r.port, branch: r.branch })), 'm', { sample: false, record: rec, width: 400, height: 300 });
t('a yes/no status reads in the source\'s words (running / stopped), coloured', /class="pill"[^>]*>stopped</.test(h) && /class="pill"[^>]*>running</.test(h) && !/>true</.test(h) && !/>false</.test(h), text(h).slice(0, 200));
t('problems first: the stopped sandbox leads', text(h).indexOf('vera-dev') < text(h).indexOf('mirror'));
t('the detail line under the name (the branch)', /<small>loop-lab\/sandbox/.test(h));
t('a figure with its bar (its share of the largest)', /class="vv"><i class="bar" style="width:\d+%"><\/i><span>8,998<\/span>/.test(h) || /class="vv"><i class="bar"/.test(h), (h.match(/class="vv"[^]*?<\/span><\/span>/) || [''])[0]);
t('the statuses as a bar with their counts, a click filters', /class="vb-r2s"/.test(h) && /data-vb-set="f:stopped"/.test(h) && /running<b>2<\/b>/.test(h));
const hf = W.draw('rows', sbx.map((r) => ({ name: r.name, status: r.running, value: r.port })), 'm', { sample: false, record: rec, width: 400, height: 300, ui: { f: 'stopped' } });
t('filtered to a status', /vera-dev/.test(hf) && !/>mirror</.test(hf));
const times = W.draw('rows', [{ name: 'a', value: new Date(Date.now() - 3 * 3600e3).toISOString() }], 'm', { sample: false, width: 400 });
t('a time reads "3 h ago"', /3 h ago/.test(times), text(times));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
