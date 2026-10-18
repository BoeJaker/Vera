// An object keyed by id (ollama.instances {gpu-250: {url, status, ...}, ...}) is read as rows: one row per key, the key
// the first column (id, or key when the records carry an id), then the records' fields - the last shape rule before json.
// Anything that is not that shape is read as it was.
//   node tests/test_widget_keyed_rows.cjs
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const R = path.join(__dirname, '..');
const WE = fs.readFileSync(path.join(R, 'vera', 'widgets', 'widget_element.js'), 'utf8');
const defined = {};
const ctx = { window: {}, console, HTMLElement: class {}, CustomEvent: class {}, customElements: { get: (n) => defined[n], define: (n, c) => { defined[n] = c; } }, document: { querySelectorAll: () => [], createElement: () => ({ setAttribute() {}, appendChild() {}, style: {} }), head: { appendChild() {} }, getElementById: () => null }, setTimeout, clearTimeout, requestAnimationFrame: (f) => setTimeout(f, 0), localStorage: { getItem: () => null, setItem() {} } };
ctx.window.customElements = ctx.customElements; ctx.window.document = ctx.document; ctx.window.localStorage = ctx.localStorage;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'ui', 'iso.js'), 'utf8'), ctx); vm.runInNewContext(WE, ctx); const W = ctx.window.VeraWidget;
vm.runInNewContext(fs.readFileSync(path.join(R, 'vera', 'widgets', 'present.js'), 'utf8'), ctx);
W.presentations([]);
const text = (h) => h.replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const J = (x) => JSON.parse(JSON.stringify(x));

const inst = (url, status, gpu) => ({ url, status, models: ['qwen3:1.7b'], in_use: 0, latency_ms: 12.5, has_gpu: gpu, num_thread: 8 });
const INSTANCES = { 'gpu-250': inst('http://10.0.0.250:11434', 'online', true), 'cpu-246': inst('http://10.0.0.246:11434', 'online', false), 'cpu-247': inst('http://10.0.0.247:11434', 'offline', false) };

// ── the shape: an ollama.instances answer ──
const r1 = W.fromCapResult('ollama.instances', J(INSTANCES));
const t1 = r1[0];
t('keyed: one table record', r1.length === 1 && t1.form === 'table', r1.map((r) => r.form).join(','));
t('keyed: one row per key, the key first as id, then the fields in order', Array.isArray(t1.data) && t1.data.length === 3 && JSON.stringify(Object.keys(t1.data[0])) === JSON.stringify(['id', 'url', 'status', 'models', 'in_use', 'latency_ms', 'has_gpu', 'num_thread']) && t1.data.map((r) => r.id).join() === 'gpu-250,cpu-246,cpu-247' && t1.data[2].status === 'offline');
t('keyed: says why, and reads again through the whole answer', /keyed by their id/.test(t1.why) && t1.read.map.rows === '$' && t1.source === 'ollama.instances');
const h1 = W.draw(t1.form, t1.data, 'l', { record: t1, height: 200, width: 600, sample: false });
t('keyed: draws as a table with the id column first and every key', /vb-tf/.test(h1) && /^id url status/.test(text(h1)) && /gpu-250[\s\S]*cpu-246[\s\S]*cpu-247/.test(text(h1)), text(h1).slice(0, 120));
const h1r = W.draw('table', J(INSTANCES), 'l', { record: t1, height: 200, width: 600, sample: false });
t('keyed: a fresh read of the same answer still draws its rows (through read.map)', /gpu-250[\s\S]*cpu-247/.test(text(h1r)) && !/no rows/.test(text(h1r)), text(h1r).slice(0, 120));
const p1 = W.present('ollama.instances', J(INSTANCES));
t('keyed: present() reads it as a table (the chat\'s table item)', p1.kind === 'table' && p1.records[0].form === 'table', p1.kind);

// records that carry their own id: the key is the column "key"
const r2 = W.fromCapResult('x.byid', { a: { id: 1, n: 'one' }, b: { id: 2, n: 'two' } })[0];
t('keyed: records with an id → the key column is "key"', r2.form === 'table' && Object.keys(r2.data[0])[0] === 'key' && r2.data[0].key === 'a' && r2.data[0].id === 1);

// ── the envelope ──
const r3 = W.fromCapResult('ollama.instances', { ok: true, count: 3, instances: J(INSTANCES) })[0];
t('envelope {ok, count, instances: {...}} → the same rows, read through that key', r3.form === 'table' && r3.data.length === 3 && r3.data[0].id === 'gpu-250' && r3.read.map.rows === 'instances');
const h3r = W.draw('table', { ok: true, count: 3, instances: J(INSTANCES) }, 'l', { record: r3, height: 200, width: 600, sample: false });
t('envelope: a fresh read of the envelope still draws its rows', /gpu-250[\s\S]*cpu-247/.test(text(h3r)), text(h3r).slice(0, 120));

// ── not this shape: unchanged ──
const form = (c) => W.fromCapResult('x.y', J(c)).map((r) => r.form).join(',');
t('a 1-key object is not read this way', form({ 'gpu-250': INSTANCES['gpu-250'] }) !== 'table' && !W.fromCapResult('x.y', { 'gpu-250': INSTANCES['gpu-250'] }).some((r) => /keyed/.test(r.why)), form({ 'gpu-250': INSTANCES['gpu-250'] }));
t('mixed values (a record beside a list) are not read this way', form({ a: { x: 1, y: 2 }, b: { x: 3, y: 4 }, c: [1, 2] }) === 'json');
t('mixed values (records beside a nested non-record) are not read this way', form({ a: { x: 1, y: 2 }, b: 'text', c: { x: 3, y: 4 }, d: { x: 5, y: 6 } }) === 'json', form({ a: { x: 1, y: 2 }, b: 'text', c: { x: 3, y: 4 }, d: { x: 5, y: 6 } }));
t('values that are arrays are not read this way', form({ a: [{ x: 1 }], b: [{ x: 2 }] }) !== 'table' || W.fromCapResult('x.y', { a: [{ x: 1 }], b: [{ x: 2 }] }).every((r) => !/keyed/.test(r.why)));
t('values of arrays of numbers stay json', form({ a: [1, 2], b: [3, 4] }) === 'json');
t('records that share too few fields are not read this way', form({ a: { p: 1, q: 2, r: 3 }, b: { s: 1, u: 2, v: 3 } }) === 'json');
t('a plain flat record stays kv', form({ host: 'gpu-250', status: 'online', latency_ms: 12 }) === 'kv');
t('named numbers stay numbers', form({ a: 1, b: 2, c: 3 }) === 'numbers');
t('a list of records stays the table it was (rows of the same shape)', W.fromCapResult('x.y', [{ name: 'a', v: 1 }, { name: 'b', v: 2 }])[0].why === 'rows of the same shape');
t('an envelope with two keyed objects is not read this way', form({ ok: true, a: J(INSTANCES), b: J(INSTANCES) }) === 'json');
t('the rule is written in the contract', WE.includes('17b keyed'));
console.log((fails ? 'FAILED ' : 'passed ') + (fails ? fails + ' check(s)' : 'all checks'));
process.exit(fails ? 1 : 0);
