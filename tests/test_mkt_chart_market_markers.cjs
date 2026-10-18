// The desk's markets chart shows every marker the markets side has for an asset (owner 2026-10-18: "markets chart should be
// able to get all the markers from market"): dated annotations, the person's trades, strategy alerts and monitor signals on
// their bars; hline annotations as price lines. Runs the real M.marketMarkers against stubbed capabilities.
const fs = require('fs'); const path = require('path'); const assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', 'vera', 'markets', 'markets_chart.js'), 'utf8');
const win = { customElements: null, document: undefined };
global.HTMLElement = global.HTMLElement || class {};   /* the chart's element class is defined at load; nothing here makes one */
new Function('window', src)(win);
const M = win.VeraMktChart;
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok  ', name); };
(async () => {
  const day = 86400, t0 = 1790000000 - (1790000000 % day); const times = Array.from({ length: 30 }, (_, i) => t0 + i * day);
  t('snap: inside, before, just past, far past', () => {
    assert.strictEqual(M.snap(times, t0 + 5 * day + 3600), t0 + 5 * day);
    assert.strictEqual(M.snap(times, t0 - 10), null);
    assert.strictEqual(M.snap(times, t0 + 29 * day + 100), t0 + 29 * day);
    assert.strictEqual(M.snap(times, t0 + 40 * day), null);
  });
  t('pyList reads the stored python-style points', () => {
    assert.deepStrictEqual(M.pyList("[{'t': 5}]"), [{ t: 5 }]);
    assert.deepStrictEqual(M.pyObj("{'a': True, 'b': None}"), { a: true, b: null });
  });
  const iso = (i) => new Date((t0 + i * day + 3600) * 1000).toISOString();
  const calls = [];
  const answers = {
    'markets.annotate.list': { annotations: [
      { kind: 'vline', points: "[{'t': " + (t0 + 3 * day) + "}]", text: 'BTC halving #4', color: '#e8b34d' },
      { kind: 'hline', points: "[{'p': 43256.49}]", text: 'support', color: '#7aa2f7' }] },
    'markets.portfolio.tx_list': { transactions: [
      { symbol_key: 'yahoo:BTC-USD', side: 'buy', qty: '0.1', price: '60000', ts: iso(4) },
      { symbol_key: 'yahoo:ETH-USD', side: 'buy', qty: '1', price: '1000', ts: iso(4) },
      { symbol_key: 'yahoo:BTC-USD', side: 'sell', qty: '0.1', price: '70000', ts: iso(9) }] },
    'markets.alerts.list': { alerts: [
      { dataset_id: 'mkt.yahoo.btc_usd.1d', direction: 'entry', name: 'VWAP Reclaim', created_at: iso(6) },
      { dataset_id: 'mkt.binance.btc_usd.1d', direction: 'exit', name: 'other asset', created_at: iso(7) }] },
    'markets.monitor.status': { monitors: [
      { dataset_id: 'mkt.yahoo.btc_usd.1h', name: 'Golden Cross', state: "{'last_entry_ts': '" + (t0 + 12 * day) + "', 'last_exit_ts': ''}" }] },
  };
  const call = async (name, args) => { calls.push([name, args]); return answers[name]; };
  const got = await M.marketMarkers(call, 'yahoo', 'BTC-USD', times);
  t('reads the four sources, read-only', () => {
    assert.deepStrictEqual(calls.map((c) => c[0]).sort(), ['markets.alerts.list', 'markets.annotate.list', 'markets.monitor.status', 'markets.portfolio.tx_list']);
    assert.strictEqual(calls.find((c) => c[0] === 'markets.annotate.list')[1].symbol_key, 'yahoo:BTC-USD');
  });
  t('every marker of this asset, on its bar; other assets left out', () => {
    const m = got.markers; const by = (txt) => m.filter((x) => String(x.text).startsWith(txt));
    assert.strictEqual(by('BTC halving').length, 1); assert.strictEqual(by('BTC halving')[0].time, t0 + 3 * day);
    assert.strictEqual(by('Buy ').length, 1); assert.strictEqual(by('Buy ')[0].time, t0 + 4 * day); assert.strictEqual(by('Buy ')[0].shape, 'arrowUp');
    assert.strictEqual(by('Sell ').length, 1); assert.strictEqual(by('Sell ')[0].shape, 'arrowDown');
    assert.strictEqual(by('VWAP Reclaim').length, 1); assert.strictEqual(by('other asset').length, 0);
    assert.strictEqual(by('Golden Cross').length, 1); assert.strictEqual(by('Golden Cross')[0].time, t0 + 12 * day);
    assert.strictEqual(m.length, 5);
  });
  t('price levels become price lines', () => {
    assert.deepStrictEqual(got.lines, [{ price: 43256.49, color: '#7aa2f7', title: 'support' }]);
  });
  const r2 = await M.marketMarkers(async (name) => (name === 'markets.alerts.list' ? null : answers[name]), 'yahoo', 'BTC-USD', times);
  t('a source that fails takes nothing else down', () => { assert.strictEqual(r2.markers.length, 4); });
  t('setMarkers keeps its 4-argument form (the Markets panel) and adds the extra markers', () => {
    let set = null; const series = { setMarkers: (mk) => { set = mk; } };
    M.setMarkers(series, [t0], [t0 + day], null); assert.strictEqual(set.length, 2);
    M.setMarkers(series, [t0], [], null, [{ time: t0 + 2 * day, shape: 'circle' }]); assert.strictEqual(set.length, 2); assert.strictEqual(set[1].shape, 'circle');
  });
  console.log('test_mkt_chart_market_markers: ' + n + ' passed');
})().catch((e) => { console.error(e); process.exit(1); });
