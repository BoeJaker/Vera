// DA-09: deep dives BY A MODEL, on demand - Summarise (llm.summarize) and Entities (fabric.entity_graph.extract_text) on
// the items that carry text (document, page, note, research report, records). Offered in the menu beside the ones read off
// the item; run only when picked; the card says it is running, shows a failure, and says when the text was cut.
//   node tests/test_desk_model_dives.cjs      (CommonJS: the gate parses js as scripts)
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };

let Cls = null; const store = {};
const ctx = {
  console: { log() {}, error() {}, warn() {} }, setTimeout: () => 0, clearTimeout: () => {},
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  customElements: { get: () => undefined, define: (n2, c) => { Cls = c; } },
  fetch: async () => { throw new Error('no fetch in this test'); },
  Promise, JSON, Object, Array, String, Number, Math, Date, Set, Map, RegExp, Error, URL, isFinite, parseFloat, parseInt,
};
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
vm.createContext(ctx); vm.runInContext(SRC, ctx, { filename: 'canvas_element.js' });
const api = ctx.VeraCanvas;
t('the rule, the text and the line are exported', api && typeof api.modelDivesOf === 'function' && typeof api.diveText === 'function' && typeof api.diveStrip === 'function');
const M = api.modelDivesOf; const ids = (l) => l.map((d) => d.id).join(',');

const LONG = 'Qwen leads for code and is small enough to run on the CPU node. Llama has its own licence; Gemma was slowest on forty prompts. ';
const doc = { key: 'md:1', type: 'markdown', content: { title: 'Models', md: '# Models\n\n' + LONG.repeat(3) } };
const note = { key: 'note:1', type: 'note', content: { text: LONG } };
const page = { key: 'source:u', type: 'source', content: { url: 'https://x.org/a', title: 'A page', text: LONG.repeat(2) } };
const rep = { key: 'research:1', type: 'research', content: { status: 'done', report: '## Findings\n\n' + LONG } };
const recs = { key: 'records:q', type: 'records', content: { title: 'Search', items: [
  { id: 'a', title: 'Qwen 3 released', snippet: 'Alibaba released Qwen 3 with open weights under Apache 2.0.', domain: 'a.com' },
  { id: 'b', title: 'Llama licence', snippet: 'Meta keeps its own licence for Llama models.', domain: 'b.org' }] } };

t('document: Summarise and Entities offered', ids(M(doc, {})) === 'summary,nlp-ents', ids(M(doc, {})));
t('note, page, research report, records: offered', [note, page, rep, recs].every((b) => ids(M(b, {})) === 'summary,nlp-ents'));
t('the dives name their caps', M(doc, {})[0].cap === 'llm.summarize' && M(doc, {})[1].cap === 'fabric.entity_graph.extract_text');
t('not for other kinds', [{ type: 'table', content: { columns: ['a'], rows: [['x'], ['y']] } }, { type: 'code', content: { code: LONG } },
  { type: 'entities', content: { groups: [] } }, { type: 'kv', content: { fields: { a: LONG } } }, { type: 'widget', content: { text: LONG } }]
  .every((b) => M(Object.assign({ key: 'k' }, b), {}).length === 0));
t('not for a short text', M({ key: 'n', type: 'note', content: { text: 'buy milk' } }, {}).length === 0);
t('not for a research run with no report yet', M({ key: 'r', type: 'research', content: { status: 'running', report: '' } }, {}).length === 0);
t('a dive card is offered no dives of its own', M({ key: 'dive:summary:md:1', type: 'markdown', content: { md: LONG, dive: { state: 'done' } } }, {}).length === 0);
t('the deterministic dives are unchanged (a note: none)', api.divesOf(note, {}).length === 0);

t('records, none open: the list\'s titles and snippets', api.diveText(recs, {}).of === 'the list' && api.diveText(recs, {}).text.includes('Qwen 3 released - Alibaba'));
const elOpen = { _recUi: { 'records:q': { open: { b: true }, body: { b: { text: 'The whole Llama licence page, as read.' } } } } };
t('records, one open: that record\'s text as read', api.diveText(recs, elOpen).of === 'the open record' && api.diveText(recs, elOpen).text === 'The whole Llama licence page, as read.');
t('a page read in reader mode: its reader text', api.diveText({ type: 'source', content: { text: 'raw', reader: { markdown: 'clean' } } }, {}).text === 'clean');

// picking one: the cap is called with the item's text, the card goes up running and is written with the answer
function mk(doc0, answer) {
  const el = Object.create(Cls.prototype); Object.defineProperty(el, 'canvasId', { value: 'cv1' });
  const calls = [], results = [];
  el._doc = { blocks: [doc0] }; el._blockOf = (k) => el._doc.blocks.find((x) => x.key === k) || null;
  el.call = async (name, args) => { calls.push([name, JSON.parse(JSON.stringify(args))]); };
  el._traceSteps = async () => ({ tid: 't-desk-test-1', close() {} });
  el.callResult = async (name, args, opts) => { results.push([name, JSON.parse(JSON.stringify(args)), opts]); if (answer instanceof Error) throw answer; return answer; };
  return { el, calls, results };
}
(async () => {
  let S = mk(doc, { summary: 'Qwen leads; Llama licence differs.', style: 'concise', src_chars: 400 });
  await S.el._modelDive('md:1', 'summary');
  t('Summarise calls llm.summarize once, with the document\'s text, on a desk trace', S.results.length === 1 && S.results[0][0] === 'llm.summarize'
    && S.results[0][1].text === doc.content.md && S.results[0][2] && /^t-desk-/.test(S.results[0][2].trace), JSON.stringify(S.results));
  t('the card goes up first, running, bound to its source', S.calls[0][0] === 'canvas.add' && S.calls[0][1].key === 'dive:summary:md:1' && S.calls[0][1].kind === 'markdown'
    && S.calls[0][1].content.dive.state === 'running' && S.calls[0][1].content.binds === 'md:1', JSON.stringify(S.calls[0]));
  const up = S.calls[1];
  t('...and is written with the summary', up && up[0] === 'canvas.update' && up[1].key === 'dive:summary:md:1' && up[1].content.md === 'Qwen leads; Llama licence differs.'
    && up[1].content.dive.state === 'done' && up[1].content.dive.ms >= 0, JSON.stringify(up));

  S = mk(note, { ok: true, nodes: [{ name: 'Qwen', type: 'ORG', props: { mention_count: 1 } }, { name: 'Llama', type: 'PRODUCT', props: { mention_count: 3 } }], edges: [] });
  await S.el._modelDive('note:1', 'nlp-ents');
  t('Entities calls the NER cap with the note\'s text as one item', S.results[0][0] === 'fabric.entity_graph.extract_text'
    && S.results[0][1].items.length === 1 && S.results[0][1].items[0].text === LONG, JSON.stringify(S.results[0]));
  const eu = S.calls[1] && S.calls[1][1].content;
  t('...and draws an entities item, most mentioned first', S.calls[0][1].kind === 'entities' && eu && eu.groups[0].entities[0].text === 'Llama' && eu.groups[0].entities[0].count === 3 && eu.dive.state === 'done', JSON.stringify(eu));

  S = mk(doc, { error: 'the model is not loaded', summary: '' });
  await S.el._modelDive('md:1', 'summary');
  t('a failure is written into the card', S.calls[1][1].content.dive.state === 'error' && /not loaded/.test(S.calls[1][1].content.dive.error));
  S = mk(doc, new Error('network down'));
  await S.el._modelDive('md:1', 'summary');
  t('a thrown call is a failure too', S.calls[1][1].content.dive.state === 'error' && /network down/.test(S.calls[1][1].content.dive.error));
  S = mk(doc, { summary: '   ' });
  await S.el._modelDive('md:1', 'summary');
  t('an empty answer is a failure, said so', S.calls[1][1].content.dive.state === 'error' && /nothing back/.test(S.calls[1][1].content.dive.error));

  const big = { key: 'md:big', type: 'markdown', content: { md: 'x'.repeat(9000) } };
  S = mk(big, { summary: 'ok' });
  await S.el._modelDive('md:big', 'summary');
  const max = api.MODEL_DIVES.find((d) => d.id === 'summary').max;
  t('a long text is cut to the dive\'s max, and the card knows', S.results[0][1].text.length === max && S.calls[0][1].content.dive.cut === true
    && S.calls[0][1].content.dive.total === 9000 && S.calls[0][1].content.dive.chars === max);

  const open = mk(Object.assign({}, doc), { summary: 'x' }); open.el._doc.blocks.push({ key: 'dive:summary:md:1', type: 'markdown', content: {} });
  await open.el._modelDive('md:1', 'summary');
  t('picked again while open: it closes, nothing runs', open.results.length === 0 && open.calls.length === 1 && open.calls[0][0] === 'canvas.remove');
  t('a dive not offered for the item does nothing', await (async () => { const z = mk({ key: 'n', type: 'note', content: { text: 'hi' } }, {}); await z.el._modelDive('n', 'summary'); return z.calls.length === 0 && z.results.length === 0; })());

  // what the card draws
  const D = api.diveStrip;
  const run = D({ cap: 'llm.summarize', of: 'the document', state: 'running', started: Date.now(), chars: 2000, total: 2000 });
  t('running: said, with what and on how much', /data-dive-state="running"/.test(run) && run.includes('llm.summarize') && run.includes('2,000'));
  const bad = D({ cap: 'llm.summarize', state: 'error', error: '<b>boom</b>' });
  t('error: shown, escaped', /data-dive-state="error"/.test(bad) && bad.includes('Failed: &lt;b&gt;boom&lt;/b&gt;'));
  const cut = D({ cap: 'llm.summarize', of: 'the document', state: 'done', ms: 2400, chars: 6000, total: 9000, cut: true });
  t('cut: said, with how much of how much', cut.includes('data-dive-cut') && cut.includes('6,000 of 9,000') && cut.includes('2.4 s'));
  t('not cut: nothing said about cutting', !D({ cap: 'x', state: 'done', ms: 5 }).includes('data-dive-cut'));
  t('a run left from a closed page says it did not finish', /data-dive-state="stale"/.test(D({ cap: 'x', state: 'running', started: Date.now() - 3600e3 })));

  // wiring: offered in the menu, routed on pick, the line drawn on the card - and nothing runs except on a pick
  t('the menu lists the model dives beside the others', SRC.includes('const mv = modelDivesOf(b, this);') && SRC.includes('data-dive-model="1"'));
  t('a pick of a model dive goes to _modelDive', SRC.includes("if (act === 'dive' && btn.dataset.diveModel)") && SRC.includes('return this._modelDive(key, btn.dataset.dive); }'));
  t('the card draws its line above its body, the body only when done', SRC.includes("if (c.dive && typeof c.dive === 'object') inner = diveStrip(c.dive) + (c.dive.state === 'done' ? inner : '');"));
  const uses = SRC.split('_modelDive(').length - 1;
  t('_modelDive is called from the pick alone (defined once, called once)', uses === 2, String(uses));
  t('no z-index literal added', !/vc-dive[^}]*z-index/.test(SRC));
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log('FAIL threw ' + (e && e.stack)); process.exit(1); });
