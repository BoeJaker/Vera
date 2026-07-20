# 15 · Markets

The Markets subsystem is a full **trading workbench**: multi-asset-class data ingestion (crypto, stocks/ETFs/indices/FX, and custom collectable series), a professional charting panel with drawing tools, a server-side indicator engine, LLM-scored market sentiment, user-buildable **ML predictors**, a strategy **backtester**, a **portfolio manager** — and an embedded Vera copilot that can drive all of it, including drawing directly on the user's chart.

It spans four capability modules under `vera/markets/`:

| Module | Layer |
|---|---|
| `markets_capabilities.py` | CCXT crypto ingestion, watchlist, job runner, auto-update scheduler, panel registration |
| `markets_data_capabilities.py` | Yahoo stocks provider, unified lookup, fast bar reads, quotes, custom asset series |
| `markets_analysis_capabilities.py` | Indicator engine (23 kinds) + per-asset configs, chart annotations, sentiment |
| `markets_lab_capabilities.py` | ML predictive tools, strategy DSL + backtester, live monitor, portfolio |
| `markets_studio_capabilities.py` | **Quant Studio** backend: strategy library, trend/regime fit, deep analytics + replay, autotune, baseline estate + market overview, key dates, macro/on-chain layers, sim accounts, saved layouts |

`ccxt`, `requests`, `numpy` and `scikit-learn` are optional dependencies — each module degrades gracefully (loads, reports unavailable) rather than breaking startup.

---

## 1. Data layer

### Providers & datasets

Every asset is addressed by a **symbol key** `provider:symbol` (e.g. `binance:BTC/USDT`, `yahoo:AAPL`, `custom:Charizard 1st Ed`) and stored as one fabric dataset per timeframe: `mkt.{provider}.{slug}.{tf}`. Bars are `fabric_records` rows with deterministic ids `{dataset_id}:{ts_ms}` (INSERT OR REPLACE ⇒ dedupe), written through the fabric single-writer queue and deliberately bypassing LLM enrichment.

- **Crypto** — CCXT (`binance`, `coinbase`, `kraken`, `bybit`), synchronous calls inside `run_in_executor`, paginated full-history backfill from 2013.
- **Stocks / ETFs / indices / FX** — Yahoo Finance v8 chart endpoints, no API key. *Gotcha:* `range=max&interval=1d` makes Yahoo silently degrade to quarterly bars — the provider always uses explicit `period1/period2`, with per-timeframe max windows for intraday (1m→7d, 5–30m→59d, 1h→729d; 1d/1wk full history).
- **Custom** (video games, collectables, trading cards) — manual price points (`markets.custom.add_price`) and CSV import; each point becomes a flat OHLC bar so charts/backtests treat them like any other series.

**Live tick recording** — per-asset toggle (`markets.live.set`, `live_track` column on the watchlist): a ~20s scheduler records the current price (ccxt ticker / yahoo 1m close) into `mkt_live_ticks` with **full retention**; `markets.live.ticks` reads the series, `markets.quotes` overlays the freshest tick, and a `markets.tick` event updates open panels.

**History audit & repair** — `markets.history.audit` reports instrument metadata including the **inception date** (yahoo `firstTradeDate`; ccxt earliest-bar probe), per-timeframe stored range, bar counts vs expected (weekend-aware for stocks), completeness % and detected **gaps**; `markets.history.repair` runs a background job that backfills from inception and re-fetches every gap range.

**Provider routing:** non-CCXT providers register ingestors in `markets_data_capabilities.PROVIDER_INGESTORS`; `markets_capabilities._ingest_timeframe` routes by exchange id, so the shared background-job runner, `markets.fetch`, and the per-asset auto-update scheduler serve every asset class.

### Key caps

| Cap | Purpose |
|---|---|
| `markets.lookup` | Unified search across yahoo + ccxt + custom, flags already-tracked assets |
| `markets.asset.add` | Provider-agnostic watchlist add + correct backfill job |
| `markets.bars` | Columnar OHLCV read in one SQL query (replaces paging /fabric/browse) |
| `markets.quotes` | Last price + day change for the whole watchlist from stored bars |
| `markets.custom.create/add_price/import_csv/list/delete` | Custom asset series |
| `markets.fetch` / `markets.jobs` / `markets.update_now` / `markets.watchlist.*` | Ingestion & watchlist (original module) |

---

## 2. Analysis layer

### Indicators

Server-side numpy engine (`compute_indicator`): `sma`, `ema`, `ribbon` (multi-EMA/SMA), `bbands`, `vwap`, `rsi` (Wilder), `stoch`, `macd`, `atr`, `obv`, `roc`. `markets.indicators` computes any set for a dataset; when called without specs it uses the asset's **saved config** (`mkt_settings` KV, key `ind:{provider}:{slug}` — keys are slug-normalised so `binance:BTC/USDT` and dataset-derived `binance:btc_usdt` hit the same row). `markets.indicator_config.get/set` is how both the UI and **Vera tweak** periods/parameters — including a per-indicator `color`; a `markets.indicators` config event makes open panels re-render.

**Custom indicators** (`markets.indicator.custom.save/list/delete/test`) — user- or Vera-authored indicators from a math expression over `o h l c v` (AST-sandboxed: whitelisted nodes only, no builtins/attributes/strings). Function library: `sma ema wilder stdev highest lowest median sum rsi atr tr vwap obv roc shift cross_up cross_dn abs log sqrt sign clip where nz`; comparisons yield 0/1 masks combinable with `&`/`|`. Stored in `mkt_custom_indicators`, merged (disabled) into every indicator config so they appear as toggleable rows in the UI, and resolvable in `markets.indicators` by their `cx_*` id.

### Annotations — how Vera draws on charts

`mkt_annotations` stores persistent drawings per symbol key: `trendline`, `ray`, `hline` (levels), `vline` (**key dates**), `rect`, `fib`, `label`, `arrow`, with points as `{t: unix_sec, p: price}`. `markets.annotate.add/list/update/remove` serve both the panel's drawing tools (author `user`, blue) and the agent (author `vera`, amber, "V" badge). Every mutation emits a `markets.annotate` event; an open panel listening on the event stream re-renders live — that is the whole "Vera draws on the chart" path.

### Sentiment

`markets.sentiment.analyze` pulls fresh headlines via `web.search`, has the LLM (`ollama_generate`, JSON mode) score −1…+1 with confidence/summary/drivers, and stores the snapshot in `mkt_sentiment` (a time series per asset). `markets.sentiment.map` returns tracked assets + fixed benchmarks (S&P 500, Nasdaq, VIX, Gold, Oil, DXY, BTC, ETH) for the heat-map; `markets.sentiment.refresh` re-scores stale entries sequentially in the background.

---

## 3. Lab layer

### ML predictive tools

`markets.ml.create` defines a model over any bar dataset: features (lagged returns, RSI, MACD-hist, stoch, BB %B, ATR, vol z-score, EMA ratio, ROC, day-of-week), horizon, task (`classify` next-N-bars direction / `regress` forward return) and model kind (`gbt`/`rf`/`logreg`/`ridge`). Training runs as a background task with **TimeSeriesSplit walk-forward validation**; metrics (accuracy/F1 + edge over the base rate, or MAE/R², plus signal Sharpe) land on the model row and stream as `markets.ml` events. The pickled pipeline is stored as a SQLite BLOB. `markets.ml.update` merges hyperparameter/feature tweaks and retrains — this is the cap Vera uses to tune models. `markets.ml.predict` returns the live signal.

### Strategies & backtesting suite

Strategies come in **three kinds** (`markets.strategy.save`):

- **rule** — JSON DSL: `entry` conditions (ANDed), `exit` conditions (ORed), each `{left, op, right}` where operands are `close`/numbers/indicator specs (`{kind:'ema',params:{n:50}}`) or `{ml:'<model_id>'}` (P(up)); ops include `crosses_above/below`.
- **ml** — `{ml_id, enter_above:0.6, exit_below:0.45}` trades a trained predictor's probability directly.
- **fused** — `members` (saved strategy ids and/or inline specs, rules + ML mixed) combined with `combine: all|any|majority` for entries and `exit_combine: any|all` — this is how rule and AI strategies fuse into one signal.

`_spec_signals` is the **single evaluator** shared by backtests and the live monitor, so what you test is exactly what gets monitored. `markets.backtest.run` takes an `engine` param: **native** (vectorised, next-open fills, intrabar SL/TP) or **backtrader** (Cerebro + SQN/DrawDown analyzers, optional dependency; `markets.backtest.engines` lists availability). Stats now include CAGR, Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, expectancy, avg win/loss, best/worst trade, avg bars in trade, exposure (+SQN on backtrader).

### Live monitoring & alerts

`markets.strategy.accept` puts a saved strategy under **live monitoring**: a 60s scheduler re-evaluates due monitors (per-strategy `interval_min`) on fresh bars from the configured dataset, tracks a virtual position in `mkt_settings` (`mon:{id}`), and raises an alert on each **new** entry/exit signal — stored in `mkt_alerts`, emitted as a `markets.alert` event, and optionally pushed to Telegram via `tg.notify` (channel `'telegram'`). `markets.monitor.status` is the monitoring dashboard; `markets.alerts.list/ack` manage the feed; `markets.strategy.archive` stops monitoring.

### Portfolio

`mkt_portfolio_tx` is a transaction ledger (buy/sell, qty, price, fees) across any symbol key including customs. `markets.portfolio.positions` does **FIFO lot accounting** → qty, avg cost, realized P&L, and values open positions from stored bars (works offline, works for collectables). `markets.portfolio.history` replays the ledger against daily closes for the value-vs-cost curve.

---

## 4. UI — the trading workbench

`markets_panel.html` (tab **Markets**) is a full-screen terminal:

- **Header** — unified asset search (crypto/stocks/collectables in one dropdown), live price + day change, sentiment chip, timeframe pills, track/refresh.
- **Left rail** — watchlist grouped by asset class with prices, change and sentiment dots; custom-asset creator; background job monitor.
- **Chart stack** — lightweight-charts candles + volume, overlay indicators (ribbon renders as a sequential teal→blue ramp), and one synced sub-pane per oscillator (RSI with 30/70, stoch with 20/80, MACD histogram+lines); panes share a logical range.
- **Drawing toolbar** — trendline/ray/hline/vline(key date)/rect/fib/label with select-move-delete, HiDPI overlay canvas mapped through fractional logical indices (`timeToX`) so shapes survive pan/zoom and can sit beyond the data edge. User drawings save through the same annotate caps Vera uses; Vera's arrive live via events.
- **Right dock** — 💬 Vera copilot (v5 agent loop via `/workshop/agent_loop/stream` + `<vera-agent-loop-output>`, markets toolkit `allowed_caps`, chart context injected into the goal), 📐 Indicators (toggle + param editors), 🤖 ML lab, 🧪 Backtest (preset/JSON strategy editor, stats grid, equity canvas, trade list), 🌡 Sentiment heat-map (diverging red→gray→teal), 💼 Portfolio.
- **Live updates** — WebSocket `/ws/mcp` + `subscribe_events` (poll `/events` fallback): fetch progress, annotations, indicator-config changes, ML training, backtests, sentiment and portfolio events all refresh in place.
- Panel-bridge action handlers (`chart_load`, `open_dock`, `reload_annotations`) + a state provider make the panel drivable from the main chat too.

---

## 5. Quant Studio (`markets_studio_capabilities.py` + `markets_studio_panel.html`)

A second tab (**Quant Studio**, `/markets/studio/panel`) purpose-built around the backtester —
its charts are a **custom canvas engine** (no third-party charting library, no watermarks) with
animated candles/indicators, a live price pulse, crosshair-synced **tiling** (1/2/3/4/6 grids),
per-tile indicator cards with inline param editing, and named **saved layouts**
(`markets.layout.save/list/delete`).

**Strategy library & visual builder** — `markets.strategy.library` ships ~18 curated templates
(trend / momentum / mean-reversion / breakout / ml) with tuning hints;
`markets.strategy.from_template` instantiates one as a saved strategy (`overrides` sets any
dotted spec path) so nobody edits JSON. The studio's builder edits rule conditions as dropdown
rows (price/indicator/ML-model/number × operator), plus ML-threshold and fusion editors, signal
preview onto the active chart, and an **ƒx indicator lab** for the sandboxed custom-indicator
expressions.

**Trend & regime fit** — `markets.analysis.trendfit`: overall OLS fit with a ±2σ regression
channel plus a piecewise fit whose segments sit at price **pivots** (Ramer–Douglas–Peucker on
log price, OLS-refit per segment) labelled bull/bear/flat by annualised slope; the 0–100
`detail` knob maps to RDP ε, so one slider goes from a 2-segment regime view to fine pivots.

**Backtest feedback, deep analytics & replay** — `markets.backtest.run` now emits per-run
progress events (`started → bars → signals → simulating → done`, all carrying the run id) that
drive a live stage checklist in the Run Center. `markets.backtest.analyze` computes drawdown
curve, longest-underwater, monthly-return heat-map, rolling Sharpe, trade histogram, streaks
and exit-reason mix from any stored run; with `replay=true` it re-simulates at full resolution
and returns per-bar equity + position + all trades so the UI can **play the backtest through
the chart** (playhead, speed control, camera-follow, trade markers popping, equity sub-pane).

**Autotune** — `markets.backtest.autotune` auto-discovers numeric spec paths, then runs a
multi-round zooming grid (coordinate descent >3 axes; span halves on improvement, widens when
stuck), persists the winner as a `[autotune]` backtest and optionally writes params back to the
strategy. Progress streams as `autotune_*` stages; `markets.backtest.autotune_status` polls.
The **agentic** counterpart is the `markets-quant` loop profile (`vera/dag/loop_profiles.py`) —
library→backtest→autotune→analyze→sim caps only, no real money.

**Baseline estate & Market Pulse** — `markets.baseline.ensure` tracks a curated estate
(SPY/QQQ/DIA/IWM, all 11 SPDR sectors, ^TNX/^VIX/DXY/TLT, gold, oil, BTC/ETH via yahoo) through
the normal watchlist auto-updater; `markets.overview` aggregates every watched asset into
per-group stats (multi-window changes, RSI, 30d vol, 52-week range position, trend label,
sparklines, breadth/medians, 60s cache) that feed the Pulse tab's animated sector **treemap**,
breadth gauges, sector strips and per-asset drill-in infographics.

**Key dates** — `markets.events.detect/apply`: BTC/LTC halvings (incl. projected), ETH
upgrades, market-wide shocks (COVID, FTX, elections, ETF approvals), and for yahoo assets the
IPO/first-trade date + dividends/splits from the chart API — written as deduped `vline`
annotations (author `events`, colour-coded) so they render on every chart.

**Macro & on-chain layers** — `markets.macro.catalog/fetch`: FRED series (fed funds, 2s/10s,
10Y–2Y, CPI + derived YoY inflation, unemployment, M2, Fed balance sheet — via the keyless
`fredgraph.csv` endpoint) and blockchain.info charts (hash rate, transactions, active
addresses, BTC supply, miner revenue, market cap) land as ordinary `mkt.macro.<slug>.1d`
datasets. A `macro` ingestor is registered into `PROVIDER_INGESTORS`, so these series ride the
shared job runner and auto-refresh through the watchlist; any chart tile layers them (≤4) on a
secondary scale via the ⧉ menu.

**Sim accounts (paper trading)** — `markets.sim.create/list/order/equity/reset/delete`:
accounts with cash + fee bps, market orders filled at the latest stored price (sized by qty,
notional or % of cash/position), hourly + per-order equity snapshots. The strategy monitor
auto-trades linked accounts (`markets.strategy.accept` gained `sim_account_id`/`sim_pct`; entry
= buy `sim_pct`% of cash, exit = close) — the safe environment for agent loops to trade in.

**ML integration** — `markets.ml.series` returns the full per-bar P(up)/forecast series so
trained models plot as chart indicators and slot into the visual builder as first-class
operands.

*(The classic Markets tab also lost the repeated TradingView logo on every indicator pane —
`attributionLogo:false`, single credit in the status bar.)*

### One panel

The **Markets tab IS the Quant Studio** — single left-rail navigation, no sub-tab bar.
The classic panel is no longer navigable; it stays served at `/markets/panel` solely so
the studio's ⛓ button can open the node-graph **pipeline builder in an overlay**
(`?pipe=1` auto-opens it). Its other unique tools are folded in: **💼 ledger** (Project
view — add/delete real portfolio transactions), **＋ custom assets** (data drawer). The
chart-workspace controls (grid, tiles, sync, animations, watch strip, data & layers) live
in a **⚙ workspace popover** on the top app bar instead of a second bar above the charts.

The studio's 🤖 **copilot dock** is dual-mode: **⟳ loop** runs the v6 agent loop as the
selected specialist (`quant-strategist` / `market-visualizer` / `indicator-smith`) with the
`markets-quant` profile and the `sys-quant-visuals` guideline skill attached. **💬 chat**
streams `/agents/chat/stream` — and since that endpoint only *lists* tool signatures, the
studio runs a **client-side tool loop**: the agent's `{"tool_use":…}` action is parsed,
executed for real (`/mcp/call` for `markets.*`/`web.search`, or local **UI tools** —
`ui.chart_load`, `ui.switch_view`, `ui.overlay_strategy`, `ui.pin_infographic`,
`ui.open_result`), the `[tool_result]` is fed back, up to 7 rounds. The same UI tools are
registered on the **panel bridge**, so any agent can drive the studio via `panel.dispatch`.
The 🧬 **self-improve loop** (markets_evolve) is surfaced in the Run Center.

### Leverage, forward-testing & multi-strategy accounts

- Specs take **`leverage`** (native engine, ≤10×): notional multiplies, cash goes negative
  (borrowed), equity marked to market — **equity ≤ 0 liquidates** (flat, `liquidated`
  stat). Excluded from autotune axes. Builder has the field; batch screening includes it.
- **⏩ forward-test** on any backtest result puts the strategy LIVE on paper: saves it if
  ad-hoc, accepts it for monitoring on its dataset and links the shared `forward-tests`
  sim account. The Run Center's **🛰 Live paper runs** card lists monitors (position,
  last signal, sim link) with one-click stop.
- **Many strategies at once**: the 🛰 **monitor launcher** (bell menu, Strategy view, or
  "＋ monitor…" on the paper-runs card) multi-selects strategies and starts a monitor for
  each — per-monitor interval, channels (in-app 🔔 + optional Telegram) and an optional
  shared sim account where each trades its own sleeve. The topbar **🔔 bell** shows the
  unseen-alert count live and lists/acks every monitor's signals.
- **Sim sleeves**: sim orders record their `source` (`strategy:<id>`, user, template,
  optimizer); exits triggered by a monitor sell only that source's sleeve — so several
  strategies, or one strategy on several timeframes, coexist on ONE account without
  liquidating each other. Positions expose the per-source breakdown (`sleeves`).
- **Metrics are separated from the market**: `markets.overview` skips `macro`/`dyn`
  watchlist rows (they never enter the Pulse market map); they render in their own
  **📊 Metrics rail** (spark cards → click opens the metric as its own chart tile).

### Shorting

The native engine simulates **long and short** (`run_backtest(arr, entry, exit_, opts,
short_entry, short_exit)`): short-sale proceeds credited on entry, liability marked to
market, inverted SL/TP, and **flips** — an opposing entry signal closes and reverses at the
same open (cross signals fire on one bar; without flips an always-in strategy could never
enter its short leg; exit reason `flip`). DSL: rule specs take `short_entry`/`short_exit`
condition lists (long-only, short-only, or both); ml specs take `short_below` /
`short_exit_above` on P(up). `_spec_signals` now returns a 4-tuple. Stats gain
`long_trades`/`short_trades`; trades carry `side`; the builder has ▲ long / ▼ short
sections; templates `supertrend-long-short` and `rsi-fade-short` ship in the library.
The backtrader engine remains long-only (guarded).

### Pivot points (pluggable)

`markets.analysis.pivots` — one output shape (`pivots:[{t,p,kind}] + line`) across methods:
**zigzag** (% reversal), **atr_zigzag** (mult×ATR, volatility-adaptive), **fractal**
(Williams), **rdp** (path simplification with the trendfit `detail` knob) — so new detectors
drop into the same 📐 chart menu and QChart layer (swing polyline + high/low diamonds).

### Projections, optimizer & rotation

- `markets.project.asset` / `markets.project.portfolio` — Monte-Carlo GBM bands
  (p10…p90) from historical drift/vol **or a strategy's backtested equity curve**
  (`strategy_map` / `strategy_backtest_id` — fees are then already inside), summed to
  portfolio level, in **nominal and real terms** (inflation auto-read from the fetched
  `fred:CPI_YOY` layer, overridable with an "on the ground" rate), with optional annual
  cost drag. The ⧗ Project view renders the fan chart + per-asset assumptions.
- `markets.portfolio.optimize` — Monte-Carlo efficient frontier over aligned daily
  returns (correlations included): max-Sharpe / max-return / min-vol weights vs current
  holdings, fee-priced rebalance plan (turnover × fee bps), and `apply='sim:<id>'`
  executes the plan as paper orders.
- `markets.rotation.scan` — the BTC→ETH "optimal path" scanner: every asset scored by
  blended momentum z-scores + trend regime + live accepted-strategy signals + optional
  ML P(up); held laggards get fee-aware switch suggestions with the pair-ratio spark.

### Causal pivots as indicators + regime (multi-stage) strategies

- Indicator kinds `pivotlevels` (last CONFIRMED pivot high/low as step levels) and
  `pivotdir` (current confirmed leg ±1) are **causal** — verified not to repaint on
  truncation — so tuned pivots are first-class, sweepable strategy operands. Library:
  `pivot-breakout` (both sides) and `pivot-trend`.
- Strategy kind **`regime`** — phased/multi-stage backtests: `{regimes:[{when:'bull|bear|
  flat|any', strategy_id|spec}], regime_source:{method:'sma|supertrend|pivots'},
  exit_on_regime_change}`. Member entries only arm in their phase; exits always work; a
  phase flip closes positions by default. Builder has a 🌗 editor; `regime-switcher`
  template ships (bull → EMA trend, bear → RSI short-fade).

### Deeper autotune

`markets.backtest.autotune` now holds out an **out-of-sample tail** (`oos_split=0.25` —
the search never sees it; the winner is re-scored on it → `stats_oos` + an "overfit risk /
holds up" verdict in the UI), rejects combos under `min_trades`, adds per-round
**random-jitter exploration** to escape local grids, and finishes with a per-axis
**sensitivity sweep** (metric across ±40% of each winning parameter) rendered as
robustness bars.

### Live strategy overlay

Every chart tile has 🎯 — pick any saved strategy and its entry/exit (and short) signals
render as live markers on the price action, with a pulsing "▲ LONG NOW / ▼ SHORT NOW"
badge when a signal fires on the latest bar; refreshes with each bar update.

### Market dynamics & OSINT

- `markets.dynamics.fetch/snapshot` — **open longs vs shorts**: Binance-futures funding
  rate, open interest, global long/short account ratio and top-trader position ratio
  (keyless). Fetched series land as `mkt.dyn.<pair>_<metric>.1d` datasets via a `dyn`
  provider ingestor (auto-refresh through the watchlist); the snapshot cap powers the
  live "⚖ Positioning" card in Pulse (funding + long%/short% bars + crowding note).
- `markets.wsb.scan` — WSB-style alpha: hot posts from retail subreddits, ticker
  mentions (watchlist symbols + known set, noise-filtered) scored by upvote weight; top
  tickers appended to daily `mkt.dyn.wsb_<ticker>.1d` series.
- `markets.news.feed` (per-asset or market headlines, cached for the Pulse dashboard,
  `map_to_chart` pins the top headline) and `markets.sentiment.to_series` (LLM sentiment
  history → dataset).
- **Backtest integration**: the rule DSL accepts external-series operands —
  `{dataset:'mkt.dyn.btc_usdt_funding.1d'}` (or a bare `'mkt.…'` string) is forward-
  filled onto the strategy's bars, so funding/OI/positioning/WSB/sentiment/macro series
  are first-class entry/exit conditions. Chart tiles layer the same datasets via ⧉, and
  metrics (hashrate, shorts, funding…) are searchable from the top search bar, opening
  as their own chart tiles.

### Compound strategies, versioning, deep tuning & the trader director

- **Weighted compounds**: `kind='fused'` supports `combine:'weighted'` + `weights:[…]` +
  `enter_threshold`/`exit_threshold` — members vote with weights, the composite fires on
  the normalised score. Weights and thresholds are ordinary numeric spec leaves, so
  autotune/sweeps optimise the composition itself; monitors/alerts evaluate the same spec.
  The fusion builder has per-member weight inputs and threshold sliders.
- **Never lose a setup**: every strategy overwrite (autotune adopt, optimise re-run,
  manual edit, evolve promotion) snapshots the previous spec automatically
  (`markets.strategy.versions` / `.revert`, ⟲ button in the builder; reverts are
  themselves snapshotted).
- **Deeper autotune**: `metric='blend'` (Sharpe-led composite with Calmar + profit-factor),
  budgets to 10 rounds × 400 evals, random exploration, min-trades gate, OOS holdout —
  plus **validation re-selection**: the top-8 in-sample finalists are re-scored on a
  held-back validation slice and the validation winner ships (`validation_pick`).
- **Backtest windows**: the run form takes friendly windows (all history / 5y / 1y / …/
  custom dates) instead of bar counts, and every result gets a full-history price strip
  whose edges you **drag** to re-run the backtest over any sub-window live
  (`markets.backtest.run save=false` — no result spam).
- **⌭ Trader director** (`markets.trader.*`) — the dream-director analogue for markets: a
  scheduled deterministic loop that (1) monitors the market, (2) rolls a strategies×assets
  **results grid** in the background (a few backtested cells per tick, plus a weighted
  layered composite of the top two strategies per asset), and (3) executes fresh signals
  from cells above `min_metric` — **sim mode** trades only its configured sim account
  (per-strategy sleeves, max-positions cap) and cannot see the real book; **real mode**
  never touches sim accounts and only raises alerts unless `real_autolog` opts into ledger
  records. Optional **LLM steering** adjusts sizing/thresholds within hard bounds every N
  ticks (it never places trades and only sees the active mode's account). Run-Center card
  drives it.
- **Workspaces & watchlist**: the topbar workspace switcher loads saved layouts or
  **account-bound workspaces** (pick a sim account / the real portfolio → its bound layout
  loads, or one is generated from its holdings; ⎘ save binds). The ★ watchlist manager
  (⚙ workspace popover) lists every tracked asset with per-asset auto-update toggles,
  untrack, ⇊ full-history fetch, and one click fetches **maximum available history on
  daily + weekly** for the entire watchlist. Opening an asset from search/drill/strip
  always creates a NEW tile; the tile grid scrolls with a 360px minimum row height; UI
  chrome uses monochrome glyphs (live emoji→glyph mapping).

### Screener, ML walk-forward, infographics, agents

- `markets.backtest.batch`(+`_status`) — every (strategy × dataset) combo backtested and
  ranked (`'library'` screens all templates; `all_watchlist` covers every tracked asset);
  optional `autotune_top` fine-tunes the N best with a 2-round zoom grid. Leaderboard
  persists (`studio:batch:last`); Run Center renders it as the 🏆 best-plays table.
- `markets.ml.walkforward` — honest out-of-sample ML backtesting: per-fold expanding-window
  retraining, prediction only on unseen bars, stitched OOS signal traded through the native
  engine (short side optional); lands as a normal backtest row (engine `ml-walkforward`).
- `markets.infographic.save/list/delete` — agents compose live infographics (panel types
  stat/spark/bars/donut/gauge/heatmap/text) that render instantly in the Pulse tab.
- Specialist agents (`vera/agents/agents.py`): **quant-strategist** (drives the
  `markets-quant` loop profile), **indicator-smith**, **market-visualizer**.

---

## See also

- [Data Fabric](./06-data-fabric.md) — where bars are stored (`mkt.*` datasets) and the single-writer queue
- [Machine Learning](./16-machine-learning.md) — the general ML workshop; markets ML tools are self-contained but share the fabric
- [Agents & Chat](./19-agents-chat.md) — the agent-loop stream the embedded copilot uses
- [Capability Framework](./01-capability-framework.md) — `markets.*` registration & events
