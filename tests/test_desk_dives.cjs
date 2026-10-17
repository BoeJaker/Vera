// WSU-42 / DA-09: deep dives as widgets - what an item can be looked into, from what it already holds (nothing fetched,
// no model): each a widget record that opens beside its source, bound to it.
//   node tests/test_desk_dives.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const SRC = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const D = V.divesOf;
t('the rule is exported', typeof D === 'function');

const rec = { key: 'r1', type: 'records', content: { title: 'Bitcoin news', items: [
  { domain: 'a.com', when: '2026-10-01T10:00:00Z', type: 'news' }, { domain: 'a.com', when: '2026-10-02T10:00:00Z', type: 'news' },
  { domain: 'b.org', when: '2026-10-02T12:00:00Z', type: 'blog' }, { domain: 'c.net', when: 'not a date' }] } };
const el = { _recEnts: { r1: { x1: [['Bitcoin', 'ORG'], ['SEC', 'ORG']], x2: [['Bitcoin', 'ORG']], x3: [['ETF', 'PRODUCT']] } } };
const rd = D(rec, el); const byId = (l, id) => l.find((d) => d.id === id);
t('records: sites ranked by pages', byId(rd, 'sites') && byId(rd, 'sites').form === 'ranked' && byId(rd, 'sites').data['a.com'] === 2, JSON.stringify(rd.map((d) => d.id)));
t('records: when written, per day in order', byId(rd, 'dates') && JSON.stringify(Object.keys(byId(rd, 'dates').data)) === '["2026-10-01","2026-10-02"]' && byId(rd, 'dates').data['2026-10-02'] === 2);
t('records: kinds as parts', byId(rd, 'kinds') && byId(rd, 'kinds').form === 'donut' && byId(rd, 'kinds').data.news === 2);
t('records: names from the records\' entities', byId(rd, 'names') && byId(rd, 'names').data.Bitcoin === 2);
t('a dive is titled by what it is and what it is of', byId(rd, 'sites').title === 'Sites · Bitcoin news');
t('one site, one day: no dive that would say nothing', D({ key: 'r2', type: 'records', content: { items: [{ domain: 'a.com', when: '2026-10-01' }, { domain: 'a.com', when: '2026-10-01' }] } }, {}).length === 0);

const tbl = { key: 't1', type: 'table', content: { title: 'Models', columns: ['model', 'score', 'licence'], rows: [['qwen', '31', 'apache'], ['r1', '28', 'mit'], ['llama', '24', 'apache']] } };
const td = D(tbl, {});
t('table: a numeric column by the first', byId(td, 'num:1') && byId(td, 'num:1').form === 'ranked' && byId(td, 'num:1').data.qwen === 31 && byId(td, 'num:1').label === 'score by model');
t('table: a text column with repeats, top values', byId(td, 'top:2') && byId(td, 'top:2').data.apache === 2);
t('table: the label column itself (all distinct) is no dive', !byId(td, 'top:0'));
const big = { key: 't2', type: 'table', content: { columns: ['i', 'v'], rows: Array.from({ length: 60 }, (_, i) => [String(i), String(i % 7)]) } };
t('table: many rows - a spread (histogram of the values)', byId(D(big, {}), 'num:1') && byId(D(big, {}), 'num:1').form === 'histogram' && byId(D(big, {}), 'num:1').data.length === 60);

const ent = { key: 'e1', type: 'entities', content: { groups: [{ group: 'reply', entities: [{ text: 'Bitcoin', type: 'ORG', count: 3 }, { text: 'SEC', type: 'ORG' }, { text: 'Gensler', type: 'PERSON', count: 2 }] }] } };
const ed = D(ent, {});
t('entities: by type', byId(ed, 'types') && byId(ed, 'types').data.ORG === 4 && byId(ed, 'types').data.PERSON === 2);
t('entities: most mentioned', byId(ed, 'names') && byId(ed, 'names').data.Bitcoin === 3);
t('other kinds, and a short note: none', D({ key: 'n', type: 'note', content: { text: 'x' } }, {}).length === 0 && D({ key: 'k', type: 'kv', content: { fields: { a: 1 } } }, {}).length === 0);

const md = '# Choosing a model\n\nQwen leads for code; Qwen is small enough. Licences matter: Qwen is Apache, Llama has its own licence.\n\n'
  + '## Shortlist\n\nQwen, Llama and Gemma were tried on forty prompts; Gemma was slowest. See https://huggingface.co/qwen and https://www.ollama.com/library and https://huggingface.co/meta.\n\n'
  + '## Licences\n\nApache for Qwen, the Llama licence for Llama, Gemma terms for Gemma.';
const dd = D({ key: 'd1', type: 'markdown', content: { title: 'Models', md } }, {});
t('document: sections by their words', byId(dd, 'sections') && Object.keys(byId(dd, 'sections').data).join('|') === 'Choosing a model|Shortlist|Licences' && byId(dd, 'sections').data.Shortlist > 10, JSON.stringify(byId(dd, 'sections')));
t('document: key terms, stop words out, links not counted as words', byId(dd, 'terms') && byId(dd, 'terms').data.qwen >= 5 && byId(dd, 'terms').data.licence >= 2 && !('https' in byId(dd, 'terms').data) && !('huggingface' in byId(dd, 'terms').data), JSON.stringify(byId(dd, 'terms') && byId(dd, 'terms').data));
t('document: links by site', byId(dd, 'links') && byId(dd, 'links').data['huggingface.co'] === 2 && byId(dd, 'links').data['ollama.com'] === 1);

t('the menu lists them, lit when open; the row toggles a bound widget item',
  SRC.includes("return dv.length ? '<hr><div class=\"hd-lbl\">Deep dive</div>'")   /* WSU-58 S6: the menu's dive entry */ && SRC.includes("const dk = 'dive:' + d.id + ':' + key;")
  && SRC.includes("return this.call('canvas.remove', { key: dk });") && SRC.includes("content: { form: d.form, title: d.title, data: d.data, binds: key, source: 'deep dive · ' + d.label } }); }"));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
