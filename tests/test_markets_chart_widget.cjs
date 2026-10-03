// canvas-enrichment slice 5: a symbol's chart as a widget - markets.chart resolves what a person names (BTC, $SPY,
// BTC/USD) to the tracked asset, answers in the candle form's rows; the catalogue draws it as candles; the surface
// index fills it from the turn's ticker
//   node tests/test_markets_chart_widget.cjs      (CommonJS: the gate parses js as scripts)
const path = require('node:path'), fs = require('node:fs');
const R = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const M = R('vera/markets/markets_data_capabilities.py'), CAT = R('vera/widgets/widget_catalog.py'), S = R('vera/canvas/surface_core.py');
let fails = 0; const t = (name, cond) => { console.log((cond ? 'ok   ' : 'FAIL ') + name); if (!cond) fails++; };
t('markets.chart is a read (no network, nothing written)', M.includes('"markets.chart", http_method="GET", http_path="/markets/chart",') && M.includes('async def cap_markets_chart(symbol: str = "", timeframe: str = "1d", limit: int = 120'));
t('...a named symbol resolves to the tracked asset (BTC is BTC-USD where that is tracked)', M.includes('want |= {base + "-USD", base + "-USDT", base + "/USDT", base + "/USD", base}') && M.includes('hit = next((x for x in quotes if str(x.get("symbol") or "").upper() in want), None)'));
t('...its stored bars by the dataset id the data layer names', M.includes('raw = await cap_markets_bars(dataset_id=_dataset_id(provider, sym, timeframe or "1d"), limit=n)'));
t('...answered as the candle form\'s rows, with the last price and the change', M.includes('rows = [{"t": raw["t"][i], "open": raw["o"][i], "high": raw["h"][i], "low": raw["l"][i], "close": raw["c"][i],') && M.includes('"change_pct": chg, "count": len(rows), "bars": rows}'));
t('...an untracked symbol says so, not a guess', M.includes('is not tracked - add it to the watchlist to chart it'));
t('the catalogue draws it as candles (ohlcv)', CAT.includes('"markets.chart": {"shape": "ohlcv", "refresh_min": "1m"},'));
t('the surface index fills a symbol parameter from the turn\'s ticker', S.includes('(re.compile(r"^(symbol|symbols|ticker|pair|symbol_key|asset)$"), "TICKER")'));
console.log(fails ? fails + ' FAILED' : 'all passed');
process.exit(fails ? 1 : 0);