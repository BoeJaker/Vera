// DU-E-json: the JSON lens (catalogue Q3) - a tree with each value's type, find that marks and opens, a key pressed shows
// its path to copy, an array of objects as a table in place; views tree / flat / validate.   node tests/test_desk_json_lens.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'json:probe';
const DATA = { ok: true, count: 3, meta: { host: 'ct126', tags: ['gpu', 'v100'], deep: { deeper: { deepest: 'needle here' } } },
  nodes: [{ id: 'ct126', load: 0.4, state: 'up' }, { id: 'ct121', load: 0.9, state: 'up' }, { id: 'ct118', load: 0.1, state: 'down' }],
  note: 'x'.repeat(300) };
const host = () => ({ dataset: {}, _live: {}, _views: {} });
const draw = (el, extra) => mod.BLOCK.json(Object.assign({ title: 'answer', data: DATA }, extra || {}), 'm', KEY, el);

{ const h = draw(host());
  t('a tree with each value\'s type', /class="vc-jt">bool</.test(h) && /class="vc-jt">num</.test(h) && /class="vc-jt">str</.test(h) && /class="vc-jt">arr·3</.test(h) && /class="vc-jt">obj·3</.test(h));
  t('keys are pressable, carrying their path', h.includes('data-act="jsel" data-jp="meta.host"') && h.includes('data-act="jsel" data-jp="nodes[1].load"'));
  t('an array of objects offers "as table"', /data-act="jtab" data-jp="nodes"[^>]*>as table</.test(h));
  t('a long string is folded with its length', h.includes('(300 chars)'));
  t('the find box and the node count', h.includes('data-json-q="json:probe"') && /\d+ nodes</.test(h));
  t('deep branches start closed', !/<details class="j-node" open><summary><button class="vc-jk[^"]*" data-act="jsel" data-jp="meta.deep.deeper"/.test(h)); }
{ const el = host(); el._jsonUi = { [KEY]: { q: 'needle', sel: '', tab: {} } }; const h = draw(el);
  t('find marks what matches and counts it', h.includes('<mark>needle</mark>') && h.includes('1 match'));
  t('find opens the branches down to a match', /<details class="j-node" open><summary><button class="vc-jk[^"]*" data-act="jsel" data-jp="meta.deep.deeper"/.test(h)); }
{ const el = host(); el._jsonUi = { [KEY]: { q: '', sel: 'nodes[1].load', tab: {} } }; const h = draw(el);
  t('a selected key shows its path with copy', h.includes('<code>nodes[1].load</code>') && h.includes('data-act="jcopy"'));
  t('and opens its branch', /<details class="j-node" open><summary><button class="vc-jk[^"]*" data-act="jsel" data-jp="nodes\[1\]"/.test(h)); }
{ const el = host(); el._jsonUi = { [KEY]: { q: '', sel: '', tab: { nodes: true } } }; const h = draw(el);
  t('an array as a table: its keys as columns, a row each', /<table class="vc-jtable"><thead><tr><th>#<\/th><th>id<\/th><th>load<\/th><th>state<\/th>/.test(h) && (h.match(/<tr><td>\d+<\/td>/g) || []).length === 3 && />as tree</.test(h)); }
{ const el = host(); el._views = { [KEY]: 'flat' }; const h = draw(el);
  t('flat: path, type and value for every node', h.includes('<table class="vc-jflat">') && h.includes('data-jp="meta.tags[1]"') && h.includes('v100')); }
{ const el = host(); el._views = { [KEY]: 'validate' };
  const ok = draw(el); const bad = mod.BLOCK.json({ title: 'raw', data: '{"a": 1,', text: '{"a": 1,' }, 'm', KEY, el);
  t('validate: a parsed answer says its size', ok.includes('parsed answer') && /\d+ values · depth \d+/.test(ok));
  t('validate: text that is not JSON says so and why', bad.includes('not valid JSON') && /class="vc-jval bad"/.test(bad)); }
t('the head offers the three views', C.includes("if (b.type === 'json') return { views: ['tree', 'flat', 'validate'], cur: viewOf(el, key, 'tree') };"));
t('the controls are wired: select, copy, as table', C.includes("if (act === 'jsel' || act === 'jtab' || act === 'jcopy') { const ui = jsonUi(this, key);") && C.includes("q.dataset.jsonQ == null"));
t('the plain tree stays for the bodies that use it', C.includes('function jsonTree(v, d) {'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
