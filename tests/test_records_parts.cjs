// The records browser in parts (DU-E-records, ESTATE-UI-REVIEW 4.5): the same drawing built from named parts that share one
// context - so a change to one part (the reader, the evidence, the pager) is made in that part.
//   node tests/test_records_parts.cjs
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok -', name); };
const items = Array.from({ length: 12 }, (_, i) => ({ id: 'u' + i, url: 'https://s' + (i % 3) + '.example/' + i, domain: 's' + (i % 3) + '.example', title: 'Page ' + i, snippet: 'says ' + i, when: '2026-10-0' + (1 + (i % 9)) }));
const draw = (c, st) => { const el = { _recUi: { k: Object.assign({ page: 0, q: '', sort: 'rank', view: 'list', dom: '', ent: '', open: {}, body: {} }, st || {}) } }; return V.BLOCK.records(c, 'm', 'k', el); };

t('BLOCK.records is the parts, composed', () => {
  assert(S.includes('    records: (c, size, key, el) => recDraw(recCtx(c, size, key, el)),'));
  ['function recCtx(c, size, key, el) {', 'function recOpened(x, r) {', 'function recBody(x) {', 'function recEvidence(x) {', 'function recGlance(x) {', 'function recDraw(x) {'].forEach((p) => assert(S.includes(p), p));
});
t('the parts draw what the browser always drew: the bar, the rows, the pager, the sites', () => {
  const h = draw({ title: 'Web', kind: 'web', items });
  assert(/class="vc-rbx k-web s-m"/.test(h) && /data-rec-q="k"/.test(h) && /vc-rbx-row/.test(h) && /vc-rbx-foot/.test(h) && /vc-rbx-glance/.test(h));
  assert.strictEqual((h.match(/class="vc-rbx-row/g) || []).length, 6, 'a page of six at m');
});
t('an opened record is the opened part; the table and cards views are the body part', () => {
  assert(/vc-rbx-open/.test(draw({ kind: 'web', items }, { open: { u1: 1 } })));
  assert(/vc-rbx-tbl/.test(draw({ kind: 'web', items }, { view: 'table' })) && /vc-rbx-cards/.test(draw({ kind: 'web', items }, { view: 'cards' })));
});
t('a lookup\'s evidence is the evidence part: its head over the pages, its foot under them', () => {
  const h = draw({ kind: 'evidence', items: items.slice(0, 3).map((r, i) => Object.assign({ n: i + 1 }, r)), evidence: { question: 'q?', subject: 's', need: 'news', answerable: true, checked: ['web'], gaps: ['one'] } });
  assert(h.indexOf('vc-ev') < h.indexOf('vc-rbx-row') && h.indexOf('vc-ev-foot') > h.indexOf('vc-rbx-row'));
});
console.log(n + ' passed');
