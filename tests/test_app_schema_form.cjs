// An app action's form from its capability's inputs (form: "schema" - the fields ui.apps.get derives): text, number,
// a select for an enum, a box for a yes/no, the description as a hint; the values sent as what they are.
//   node tests/test_app_schema_form.cjs
'use strict';
const fs = require('fs'), path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const FIELDS = [{ key: 'symbol', label: 'Symbol', required: true }, { key: 'exchange', label: 'Exchange', default: 'binance', title: 'Where it trades' },
  { key: 'backfill', label: 'Backfill', type: 'checkbox', default: true }, { key: 'days', label: 'Days', type: 'number' }, { key: 'tf', label: 'Tf', type: 'select', options: ['1h', '1d'], default: '1d' }];
const h = V.BLOCK.app({ app: 'm.w' }, 'm', 'app:w', { _apps: { 'app:w': { manifest: { title: 'W', read: { cap: 'm.w.list' }, actions: [{ id: 'add', label: 'Add', cap: 'm.w.add', form: 'schema', fields: FIELDS }] },
  cap: 'm.w.list', args: {}, result: { rows: [{ symbol: 'A' }] }, at: Date.now(), ui: { form: { aid: 'add', row: null } } } } });
t('a required text field', /<input class="vc-app-f" data-app-f="symbol" type="text" placeholder="Symbol \*" value=""/.test(h));
t('a default and the description as a hint', /data-app-f="exchange" title="Where it trades" type="text" placeholder="Exchange" value="binance"/.test(h));
t('a yes/no is a box, ticked by its default', /<label class="vc-app-fc"><input class="vc-app-f" data-app-f="backfill" data-app-ft="checkbox" type="checkbox" checked> Backfill<\/label>/.test(h));
t('a number', /data-app-f="days" data-app-ft="number" type="number"/.test(h));
t('an enum is a select with its default chosen', /<select class="vc-app-f" data-app-f="tf" aria-label="Tf"><option value="">Tf<\/option><option>1h<\/option><option selected>1d<\/option><\/select>/.test(h));
t('values go as what they are (a box: true/false, a number: a number)', S.includes("vals[x.dataset.appF] = ft === 'checkbox' ? x.checked : ft === 'number' && x.value !== '' && isFinite(+x.value) ? +x.value : x.value;"));
const plain = V.BLOCK.app({ app: 'm.w' }, 'm', 'app:w', { _apps: { 'app:w': { manifest: { title: 'W', read: { cap: 'm.w.list' }, actions: [{ id: 'add', label: 'Add', cap: 'm.w.add', fields: [{ key: 'symbol', label: 'Symbol' }] }] },
  cap: 'm.w.list', args: {}, result: { rows: [{ symbol: 'A' }] }, at: Date.now(), ui: { form: { aid: 'add', row: null } } } } });
t('a written text field is drawn as before', /<input class="vc-app-f" data-app-f="symbol" type="text" placeholder="Symbol" value="" spellcheck="false">/.test(plain));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
