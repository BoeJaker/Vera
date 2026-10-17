// DA-08a: an app's actions act on the thing (DESK-SPINE-DESIGN §7) - a row being removed goes at once and comes back if
// the action fails; a changed row is patched from the answer; a new row is lit when it arrives; a removal the manifest can
// reverse offers Undo for 10 s; a confirm asks in place for 4 s. Nothing lands as an item.
//   node tests/test_app_actions_in_place.cjs
'use strict';
const fs = require('fs'), path = require('path');
const FILE = path.join(__dirname, '..', 'vera', 'canvas', 'canvas_element.js');
const V = require(FILE); const S = fs.readFileSync(FILE, 'utf8');
let fails = 0; const t = (name, cond, extra) => { console.log((cond ? 'ok   ' : 'FAIL ') + name + (cond ? '' : '  ' + (extra || ''))); if (!cond) fails++; };
const ROWS = [{ symbol: 'BTC-USD', exchange: 'binance', bars: 10 }, { symbol: 'ETH-USD', exchange: 'binance', bars: 8 }, { symbol: 'SPXL', exchange: 'yahoo', bars: 5 }];
const MAN = { panel: 'markets', title: 'Watchlist', read: { cap: 'markets.watchlist.list' }, actions: [{ id: 'remove', label: 'Remove', cap: 'markets.watchlist.remove', where: 'row', confirm: true, undo: 'markets.watchlist.add' }] };
const draw = (ui) => { const el = { _apps: { 'app:w': { manifest: MAN, cap: 'markets.watchlist.list', args: {}, result: { rows: ROWS.map((r) => Object.assign({}, r)) }, at: Date.now(), ui } } }; return V.BLOCK.app({ app: 'markets.watchlist' }, 'm', 'app:w', el); };
const rowOf = (h, sym) => { const m = h.match(new RegExp('<tr data-app-row="\\d+"( class="[^"]*")?><td>' + sym.replace(/[-]/g, '\\-'))); return m ? (m[1] || '') : null; };

{ const h = draw({ q: '', sort: null, gone: { 'symbol=ETH-USD@binance': true } });
  t('a row being removed is marked going (at once, before the answer)', / class="gone"/.test(rowOf(h, 'ETH-USD') || '') && rowOf(h, 'BTC-USD') === ''); }
{ const h = draw({ q: '', sort: null, hl: { 'symbol=SPXL@yahoo': Date.now() } });
  t('a new or changed row is lit', / class="hl"/.test(rowOf(h, 'SPXL') || ''));
  const old = draw({ q: '', sort: null, hl: { 'symbol=SPXL@yahoo': Date.now() - 9000 } });
  t('...for a few seconds only', rowOf(old, 'SPXL') === ''); }
{ const h = draw({ q: '', sort: null, msg: { ok: true, text: 'Remove: done' }, undo: { a: MAN.actions[0], row: ROWS[1], until: Date.now() + 9000 } });
  t('after a removal the manifest can reverse: Undo in the bar', /Remove: done<\/span><button type="button" class="hx on" data-app-undo="1" title="Put it back \(markets\.watchlist\.add\)">Undo<\/button>/.test(h));
  const late = draw({ q: '', sort: null, msg: { ok: true, text: 'Remove: done' }, undo: { a: MAN.actions[0], row: ROWS[1], until: Date.now() - 1 } });
  t('...for 10 s', !/data-app-undo/.test(late)); }
// the run
t('remove: marked going before the call; put back when it fails', S.includes("if (eff === 'remove' && rk) (ui.gone || (ui.gone = {}))[rk] = true;") && S.includes("if (!ui.msg.ok) { if (rk && ui.gone) delete ui.gone[rk]; ui.before = null; return this._appDraw(key); }"));
t('an answer that says what changed patches the row and lights it', S.includes("if (res && res.changed && typeof res.changed === 'object' && row) { Object.assign(row, res.changed); (ui.hl || (ui.hl = {}))[rk] = Date.now(); }"));
t('create: the rows before are kept, so the new one is lit when the read brings it', S.includes("if (eff === 'create') ui.before = new Set((st.rows || []).map((x) => appRowKey(st, x)));") && S.includes("if (ui.before) { keys.forEach((k) => { if (!ui.before.has(k)) (ui.hl || (ui.hl = {}))[k] = Date.now(); }); ui.before = null; }"));
t('undo: the manifest\'s inverse with the gone row\'s values, then read again', S.includes("res = await this.callResult(u.a.undo, appActArgs({ args: u.a.undo_args || u.a.args || {} }, u.row, appArgs, {}));") && S.includes("if (t.closest('[data-app-undo]')) return this._appUndo(key);"));
t('the Undo button reaches its handler (the click guard of the bar lets it through)', S.includes("t.closest('[data-app-cancel],[data-app-run],[data-app-yes],[data-app-rowact],[data-app-undo]')"));
t('a confirm asks for 4 s', S.includes("st.confirmT = setTimeout(() => { if (ui.confirm && ui.confirm.aid === aid) { ui.confirm = null; this._appDraw(key); } }, 4000);"));
t('a row is known by live.key, else id / symbol (+ exchange) / name / key / title', S.includes("const f = lk && x[lk] != null ? lk : ['id', 'symbol', 'name', 'key', 'title'].find((n) => x[n] != null && x[n] !== '');"));
// the manifests that can put a removal back
for (const [f, id, undo] of [['markets', 'markets.watchlist', 'markets.watchlist.add'], ['calendar-panel', 'calendar.agenda', 'cal.event.upsert']]) {
  const m = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'vera', 'apps', f + '.json'), 'utf8')); const app = (m.apps || m).find((a) => a.id === id);
  const rm = app && (app.actions || []).find((a) => a.effect === 'remove');
  t(id + ': its removal says how to put it back', !!(rm && rm.undo === undo && rm.undo_args && Object.keys(rm.undo_args).length >= 2)); }
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);
