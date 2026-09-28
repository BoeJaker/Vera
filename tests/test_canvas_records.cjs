// The records browser on the canvas (vera/canvas/canvas_element.js BLOCK.records).
//
// "There needs to be more ways to browse through multiple records like research or search results on news and
// interesting extracted data on the canvas and have it presented in a well styled, informative and easy to
// navigate way."
//
// A list you move through: it holds EVERY record (the eleventh search result used to be unreachable), pages sized
// to the item, filters, facets by domain, sorts, three views, and a record that opens in place. The browser's
// state is the element's, per item - paging is not an edit of the canvas.
//
//   node tests/test_canvas_records.cjs
const path = require('node:path');
let fails = 0;
const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
t('the records block ships', !!(mod && mod.BLOCK && typeof mod.BLOCK.records === 'function'));
if (!mod || !mod.BLOCK || !mod.BLOCK.records) { console.log(fails + ' FAILED'); process.exit(1); }

const items = Array.from({ length: 43 }, (_, i) => ({ id: 'u' + i, url: 'https://' + (i % 3 ? 'coindesk.com' : 'reuters.com') + '/a' + i,
  domain: i % 3 ? 'coindesk.com' : 'reuters.com', title: (i === 7 ? 'Halving ' : 'Story ') + i, snippet: 'about ' + i,
  when: new Date(Date.UTC(2026, 8, 1 + (i % 27))).toISOString(), score: 1 - i / 50 }));
const C = { title: 'Web · crypto', kind: 'web', source: 'web.search', items, total: items.length };
const el = {};
const draw = (size) => mod.BLOCK.records(C, size || 'm', 'enrich:web.search:x', el);
const st = () => el._recUi['enrich:web.search:x'];

{
  const h = draw('m');
  t('the header says how many it holds', /43 records/.test(h));
  t('an m item shows a page of six', (h.match(/class="vc-rbx-row/g) || []).length === 6, String((h.match(/class="vc-rbx-row/g) || []).length));
  t('and says where it is in the whole', /1–6 of 43/.test(h));
  t('an l item pages by ten', (draw('l').match(/class="vc-rbx-row/g) || []).length === 10);
  t('the domains are facets, busiest first', /data-rec-act="dom"[^>]*data-rec-arg="coindesk.com"/.test(h) && h.indexOf('coindesk.com<i>') < h.indexOf('reuters.com<i>'));
  t('a record with a url is a link that opens elsewhere', /<a class="vc-rbx-t" href="https:\/\/reuters.com\/a0" target="_blank"/.test(h));
  t('relevance is drawn, not printed', /vc-rbx-sc/.test(h));
}
{
  st().page = 7; const h = draw('m');   // 43 / 6 = 8 pages; the last holds one
  t('the last page holds what is left', (h.match(/class="vc-rbx-row/g) || []).length === 1 && /43–43 of 43/.test(h));
  st().page = 99; t('a page past the end is the last page', /43–43 of 43/.test(draw('m')));
  st().page = 0;
}
{
  st().q = 'halving'; const h = draw('m');
  t('the filter narrows, and says of how many', (h.match(/class="vc-rbx-row/g) || []).length === 1 && /\(of 43\)/.test(h));
  st().q = 'zzz'; t('a filter that matches nothing says so', /nothing matches/.test(draw('m')));
  st().q = ''; st().dom = 'reuters.com';
  t('a facet narrows to its domain', /1–6 of 15 \(of 43\)/.test(draw('m')));
  st().dom = '';
}
{
  st().sort = 'newest'; const h = draw('m');
  const first = (h.match(/Story \d+|Halving \d+/) || [''])[0];
  t('newest first puts the latest date on top', first === 'Story 26' || first === 'Story 53', first);
  st().sort = 'title'; t('A-Z sorts by title', (draw('m').match(/(Halving|Story) \d+/) || [''])[0] === 'Halving 7');
  st().sort = 'rank';
}
{
  st().view = 'cards'; t('cards is a grid', /vc-rbx-cards/.test(draw('l')));
  st().view = 'table'; const h = draw('l');
  t('table is a table with a score column', /vc-rbx-tbl/.test(h) && /<th>score<\/th>/.test(h));
  st().view = 'list';
}
{
  st().open = { u3: true }; st().body = { u3: { text: '# The page\n\nits text' } }; const h = draw('m');
  t('an opened record shows its actions', /data-rec-act="read"[^>]*data-rec-arg="u3"/.test(h) && /data-rec-act="land"/.test(h));
  t('and what reading it fetched', /vc-rbx-body/.test(h) && /The page/.test(h));
  st().open = {}; st().body = {};
}
{
  const fab = { kind: 'memory', title: 'From the fabric', items: [{ id: 'r1', title: 'Halving notes', snippet: 'x', ref: { record_id: 'r1', dataset_id: 'd' }, meta: { dataset_id: 'd' } }] };
  const e2 = { _recUi: { k: { page: 0, q: '', sort: 'rank', view: 'list', dom: '', open: { r1: true }, body: { r1: { text: 'part one', next: 6000 } } } } };
  const h = mod.BLOCK.records(fab, 'm', 'k', e2);
  t('a fabric record reads through memory.read, and reads on', /data-rec-act="rec"/.test(h) && /data-rec-act="more"/.test(h));
}
{
  t('an empty list says so', /nothing in it/.test(mod.BLOCK.records({ items: [] }, 'm', 'z', {})));
  t('a renderer called with no element still draws', /vc-rbx/.test(mod.BLOCK.records(C, 'm', 'noel', null)));
  t('a list with more to fetch offers it', /data-rec-act="loadmore"/.test(mod.BLOCK.records(Object.assign({}, C, { next: { cap: 'web.search', args: { page: 2 } } }), 'm', 'n', {})));
}
console.log(fails ? fails + ' FAILED' : 'all passed'); process.exit(fails ? 1 : 0);
