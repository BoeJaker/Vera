// DU-E-table: the table bar - sort by a header (numbers as numbers; again the other way; a third time the answer's
// order), a filter with the row count, a column picker, copy and CSV of the rows shown, a sticky head.
//   node tests/test_desk_table_bar.cjs
const fs = require('node:fs'), path = require('node:path');
const stub = () => ({ style: {}, dataset: {}, setAttribute() {}, appendChild() {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {} });
globalThis.document = { createElement: stub, querySelector: () => null, querySelectorAll: () => [], head: stub(), body: stub(), addEventListener() {} };
const mod = require(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'));
const C = fs.readFileSync(path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js'), 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const KEY = 'table:probe';
const ROWS = Array.from({ length: 80 }, (_, i) => ['ct' + (100 + ((i * 37) % 80)), String((i * 13) % 97) + (i % 3 ? '' : '.5'), i % 4 ? 'up' : 'down', '1,' + String(100 + i).padStart(3, '0')]);
const TABLE = { caption: 'nodes', columns: ['node', 'load', 'state', 'bytes'], rows: ROWS };
const host = (ui, view) => { const h = { dataset: {}, _live: {}, _views: { [KEY]: view || 'table' } }; if (ui) h._tblUi = { [KEY]: Object.assign({ sort: -1, dir: 1, q: '', hide: {}, cols: false }, ui) }; return h; };
const draw = (el) => mod.BLOCK.table(JSON.parse(JSON.stringify(TABLE)), 'l', KEY, el);
const bodyRows = (h) => [...h.matchAll(/<tr><td>([\s\S]*?)<\/tr>/g)].map((m) => m[1].split('</td><td>').map((x) => x.replace(/<\/?[^>]+>/g, '')));

{ const h = draw(host());
  t('the bar: filter, row count, columns, copy, CSV', h.includes('data-tbl-q="table:probe"') && h.includes('80 rows') && h.includes('data-act="tblcols"') && h.includes('data-act="tblcopy"') && h.includes('data-act="tblcsv"'));
  t('each header sorts', (h.match(/data-act="tblsort"/g) || []).length === 4);
  t('the body is still the desk\'s table wrap (its sizing rules hold)', h.startsWith('<div class="vc-tablewrap vc-tbarwrap">'));
  t('every row drawn, in the answer\'s order', bodyRows(h).length === 80 && bodyRows(h)[0][0] === ROWS[0][0]); }
{ const r = bodyRows(draw(host({ sort: 1, dir: 1 }))).map((x) => parseFloat(x[1]));
  t('sorted by a number column: as numbers, rising', r.every((v, i) => i === 0 || r[i - 1] <= v), r.slice(0, 6).join(','));
  const d = bodyRows(draw(host({ sort: 1, dir: -1 }))).map((x) => parseFloat(x[1]));
  t('and falling', d.every((v, i) => i === 0 || d[i - 1] >= v));
  const b = bodyRows(draw(host({ sort: 3, dir: 1 }))).map((x) => +x[3].replace(/,/g, ''));
  t('numbers with thousands separators sort as numbers', b.every((v, i) => i === 0 || b[i - 1] <= v));
  const n = bodyRows(draw(host({ sort: 0, dir: 1 }))).map((x) => x[0]);
  t('text sorts naturally', n.every((v, i) => i === 0 || n[i - 1].localeCompare(v, undefined, { numeric: true }) <= 0));
  t('the sorted header says which way', draw(host({ sort: 1, dir: -1 })).includes('load ▼')); }
{ const h = draw(host({ q: 'down' })); const r = bodyRows(h);
  t('the filter keeps the rows with it and counts them', r.length === 20 && r.every((x) => x[2] === 'down') && h.includes('20 of 80 rows') && h.includes('<mark>down</mark>'));
  t('no row: it says so', draw(host({ q: 'zzz' })).includes('no row has "zzz"')); }
{ const h = draw(host({ hide: { 3: true }, cols: true })); const r = bodyRows(h);
  t('a hidden column is gone from the head and the rows', !/data-col="3"[^>]*>bytes/.test(h.split('<table')[1]) && r[0].length === 3);
  t('the picker lists every column, the hidden one unticked', (h.match(/data-act="tblcol"/g) || []).length === 4 && /data-col="3">bytes/.test(h) && /data-col="0" checked>node/.test(h)); }
t('the other views stay (list on a desk by default, cards, chart)', draw(host(null, 'list')).includes('vc-tv-list') && draw(host(null, 'cards')).includes('vc-tv-cards'));
t('copy and CSV are the rows shown, in the order shown, without hidden columns', C.includes("tblText(c, ui, '\\t')") && C.includes("tblText(c, ui, ',')") && C.includes('return [cols.filter((_, j) => keep[j]).map(cell).join(sep)].concat(tblRows(c, ui).map('));
t('the filter box keeps its focus across the redraw', C.includes('q.dataset.tblQ == null') && C.includes("body.querySelector('[data-tbl-q=\"'"));
t('never no columns', C.includes('if ((c.columns || []).every((_, k) => ui.hide[k])) ui.hide[j] = false;'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
