# 15 · Markets

The Markets subsystem is a full **trading workbench**. It provides:

- multi-asset data ingestion: crypto, stocks/ETFs/indices/FX, custom collectable
  series, macro and on-chain series, and market-positioning series
- a custom-canvas charting studio with drawing tools
- a server-side indicator engine with user-defined indicators
- LLM-scored market sentiment and news OSINT
- user-buildable **ML predictors**
- a strategy **backtester** with autotune, screening and walk-forward ML
- live strategy monitoring with alerts
- paper-trading **sim accounts** and a real **portfolio ledger** with UK CGT estimates
- guarded links to **real broker accounts**
- a self-improvement loop and a scheduled trader director
- an embedded Vera copilot that can drive all of it, including drawing directly
  on the user's chart

It spans six capability modules under `vera/markets/` (about 130 capabilities in
total). The **Markets** tab *is* the Quant Studio (`/markets/studio/panel`).

**Optional dependencies.** `ccxt`, `requests`, `numpy`, `scikit-learn` and
`backtrader` are optional. Each module degrades gracefully: it loads and reports
the dependency as unavailable, rather than breaking start-up.

> [!WARNING]
> Almost everything here is analysis, simulation or paper trading. The exceptions
> are `markets.broker.order`, which places **real orders** on a linked exchange
> or broker, and real-mode `markets.trader` ledger records. Broker accounts are
> linked read-only, and an order needs trading enabled on the account **and**
> `confirm=true` ([§6](#6-brokers-and-real-accounts)).

## Contents

- [Module map](#module-map)
- [1. Data layer](#1-data-layer)
  - [Providers and datasets](#providers-and-datasets)
  - [Live ticks and history repair](#live-ticks-and-history-repair)
  - [Data capabilities](#data-capabilities)
- [2. Analysis layer](#2-analysis-layer)
  - [Indicators](#indicators)
  - [Annotations — how Vera draws on charts](#annotations--how-vera-draws-on-charts)
  - [Sentiment](#sentiment)
- [3. Lab layer](#3-lab-layer)
  - [ML predictive tools](#ml-predictive-tools)
  - [Strategies and backtesting](#strategies-and-backtesting)
  - [Live monitoring and alerts](#live-monitoring-and-alerts)
  - [Portfolio and UK CGT](#portfolio-and-uk-cgt)
- [4. Classic workbench panel](#4-classic-workbench-panel)
- [5. Quant Studio](#5-quant-studio)
  - [One panel](#one-panel)
  - [Leverage, forward-testing and multi-strategy accounts](#leverage-forward-testing-and-multi-strategy-accounts)
  - [Shorting](#shorting)
  - [Pivot points (pluggable)](#pivot-points-pluggable)
  - [Projections, optimizer and rotation](#projections-optimizer-and-rotation)
  - [Causal pivots and regime strategies](#causal-pivots-and-regime-strategies)
  - [Deeper autotune](#deeper-autotune)
  - [Live strategy overlay](#live-strategy-overlay)
  - [Market dynamics, news and OSINT](#market-dynamics-news-and-osint)
  - [Compound strategies, versioning and the trader director](#compound-strategies-versioning-and-the-trader-director)
  - [Screener, ML walk-forward, infographics and agents](#screener-ml-walk-forward-infographics-and-agents)
- [6. Brokers and real accounts](#6-brokers-and-real-accounts)
- [7. Self-improvement loop](#7-self-improvement-loop)
- [8. Capability index](#8-capability-index)
- [9. Storage, schedules and events](#9-storage-schedules-and-events)
- [10. Troubleshooting](#10-troubleshooting)
- [See also](#see-also)

---

## Module map

| Module | Layer |
|---|---|
| `markets_capabilities.py` | CCXT crypto ingestion, watchlist, background job runner, auto-update scheduler, broker links, panel registration |
| `markets_data_capabilities.py` | Yahoo stocks provider, unified lookup, fast bar reads, quotes, custom asset series, live tick recording, history audit and repair |
| `markets_analysis_capabilities.py` | Indicator engine (25 kinds) and per-asset configs, custom indicators, chart annotations, sentiment |
| `markets_lab_capabilities.py` | ML predictive tools, strategy DSL, backtester and sweeps, live monitor and alerts, portfolio ledger, UK CGT |
| `markets_studio_capabilities.py` | **Quant Studio** backend: strategy library, trend and regime fit, pivots, deep analytics and replay, autotune, batch screening, ML walk-forward, baseline estate and market overview, key dates, macro and on-chain layers, dynamics and OSINT, news, projections and optimizer, sim accounts, strategy versions, trader director, infographics, saved layouts |
| `markets_evolve_capabilities.py` | The perpetual markets self-improvement loop (`markets.evolve.*`) |

---

## 1. Data layer

### Providers and datasets

Every asset is addressed by a **symbol key** of the form `provider:symbol`, for
example `binance:BTC/USDT`, `yahoo:AAPL` or `custom:Charizard 1st Ed`.

Each asset is stored as one fabric dataset per timeframe: `mkt.{provider}.{slug}.{tf}`.

- **Bars** are `fabric_records` rows with deterministic ids `{dataset_id}:{ts_ms}`.
  `INSERT OR REPLACE` therefore dedupes, so re-ingesting never duplicates.
- **Writes** go through the fabric single-writer queue, and deliberately bypass
  LLM enrichment.

The providers are:

- **Crypto** — CCXT. The default exchanges are `binance`, `coinbase`, `kraken`
  and `bybit`. Calls are synchronous inside `run_in_executor`, and a full-history
  backfill is paginated from 2013.
- **Stocks / ETFs / indices / FX** — Yahoo Finance v8 chart endpoints, with no
  API key.
  - *Gotcha:* `range=max&interval=1d` makes Yahoo silently degrade to quarterly
    bars. The provider therefore always uses explicit `period1`/`period2`.
  - Intraday timeframes have per-timeframe maximum windows: 1m → 7d, 5–30m → 59d,
    1h → 729d. 1d and 1wk get the full history.
- **Custom** (video games, collectables, trading cards) — manual price points
  (`markets.custom.add_price`) and CSV import. Each point becomes a flat OHLC bar,
  so charts and backtests treat it like any other series.
- **Macro** (`mkt.macro.<slug>.1d`) and **dynamics** (`mkt.dyn.<pair>_<metric>.1d`)
  — see [§5](#market-dynamics-news-and-osint).

**Provider routing.** Non-CCXT providers register ingestors in
`markets_data_capabilities.PROVIDER_INGESTORS`. `markets_capabilities._ingest_timeframe`
routes by exchange id, so the shared background-job runner, `markets.fetch` and
the per-asset auto-update scheduler serve every asset class.

### Live ticks and history repair

**Live tick recording** is a per-asset toggle (`markets.live.set`, the
`live_track` column on the watchlist).

- A **20-second** scheduler records the current price (CCXT ticker, or the Yahoo
  1m close) into `mkt_live_ticks`, with **full retention**.
- `markets.live.ticks` reads the series.
- `markets.quotes` overlays the freshest tick.
- A `markets.tick` event updates open panels.

**History audit and repair.** `markets.history.audit` reports:

- instrument metadata, including the **inception date** (Yahoo `firstTradeDate`,
  or the earliest CCXT bar)
- the stored range per timeframe
- bar counts against the expected count (weekend-aware for stocks)
- completeness %
- detected **gaps**

`markets.history.repair` runs a background job that backfills from inception and
re-fetches every gap range.

### Data capabilities

| Capability | Purpose |
|---|---|
| `markets.lookup` | Unified search across Yahoo, CCXT and custom; flags assets already tracked |
| `markets.asset.add` | Provider-agnostic watchlist add, plus the correct backfill job |
| `markets.bars` | Columnar OHLCV read in one SQL query |
| `markets.quotes` | Last price and day change for the whole watchlist, from stored bars and live ticks |
| `markets.custom.create` / `add_price` / `import_csv` / `list` / `delete` | Custom asset series |
| `markets.live.set` / `markets.live.ticks` | Live tick recording |
| `markets.history.audit` / `markets.history.repair` | Completeness audit and gap repair |
| `markets.exchanges` / `markets.timeframes` / `markets.symbols` | CCXT exchange, timeframe and symbol discovery |
| `markets.fetch` / `markets.jobs` / `markets.update_now` | Background backfill or refresh jobs (return a `job_id` immediately) and their status |
| `markets.watchlist.list` / `add` / `config` / `remove` | The watchlist: auto-update flag, interval (default 60 min), timeframes (default `1d`, `1h`); `delete_data` on remove |

---

## 2. Analysis layer

### Indicators

The server-side numpy engine (`compute_indicator`) supports **25 kinds**:

| Group | Kinds |
|---|---|
| Moving averages and overlays (main pane) | `sma`, `ema`, `hma`, `tema`, `ribbon` (multi-EMA/SMA), `bbands`, `keltner`, `donchian`, `supertrend`, `ichimoku`, `psar`, `vwap`, `pivotlevels` |
| Oscillators (sub-panes) | `rsi` (Wilder), `stoch`, `macd`, `atr`, `adx`, `cci`, `mfi`, `willr`, `zscore`, `obv`, `roc`, `pivotdir` |

`markets.indicators` computes any set of indicators for a dataset. Called without
specs, it uses the asset's **saved config**:

- The config lives in the `mkt_settings` KV table under key `ind:{provider}:{slug}`.
- Keys are slug-normalised, so `binance:BTC/USDT` and the dataset-derived
  `binance:btc_usdt` hit the same row.

`markets.indicator_config.get/set` is how both the UI and Vera tweak periods and
parameters, including a per-indicator `color`. A `markets.indicators` config
event makes open panels re-render.

**Custom indicators** (`markets.indicator.custom.save/list/delete/test`) are
user- or Vera-authored indicators, written as a math expression over `o h l c v`.

- **Sandboxing.** Expressions are AST-sandboxed: only whitelisted nodes are
  allowed, with no builtins, attributes or strings.
- **Function library.** `sma ema wilder stdev highest lowest median sum rsi atr
  tr vwap obv roc shift cross_up cross_dn abs log sqrt sign clip where nz`.
  Comparisons yield 0/1 masks that can be combined with `&` and `|`.
- **Integration.** Custom indicators are stored in `mkt_custom_indicators` and
  merged (disabled) into every indicator config, so they appear as toggleable
  rows in the UI. `markets.indicators` resolves them by their `cx_*` id.

### Annotations — how Vera draws on charts

`mkt_annotations` stores persistent drawings per symbol key. The kinds are
`trendline`, `ray`, `hline` (levels), `vline` (**key dates**), `rect`, `fib`,
`label` and `arrow`. Points are `{t: unix_sec, p: price}`.

`markets.annotate.add/list/update/remove` serve both:

- the panel's drawing tools (author `user`, drawn in blue)
- the agent (author `vera`, drawn in amber with a "V" badge)

Every change emits a `markets.annotate` event, and an open panel listening on
the event stream re-renders live. That is the whole "Vera draws on the chart" path.

### Sentiment

`markets.sentiment.analyze` works in three steps:

1. It pulls fresh headlines through `web.search`.
2. The LLM (`ollama_generate`, JSON mode) scores them from −1 to +1, with a
   confidence, a summary and the main drivers.
3. The snapshot is stored in `mkt_sentiment`, which keeps a time series per asset.

Related capabilities:

- `markets.sentiment.history` reads that series.
- `markets.sentiment.map` returns tracked assets plus fixed benchmarks (S&P 500,
  Nasdaq, VIX, Gold, Oil, DXY, BTC, ETH) for the heat-map.
- `markets.sentiment.refresh` re-scores stale entries one after another in the
  background.
- `markets.sentiment.to_series` turns the history into a dataset that strategies
  can use.

---

## 3. Lab layer

### ML predictive tools

`markets.ml.create` defines a model over any bar dataset:

| Setting | Options |
|---|---|
| Features | lagged returns, RSI, MACD histogram, stochastic, BB %B, ATR, volume z-score, EMA ratio, ROC, day-of-week |
| Horizon | Number of bars ahead |
| Task | `classify` (direction over the next N bars) or `regress` (forward return) |
| Model kind | `gbt`, `rf`, `logreg` or `ridge` |

Training runs as a background task with **TimeSeriesSplit walk-forward
validation**. Metrics land on the model row and stream as `markets.ml` events:
accuracy/F1 plus the edge over the base rate (or MAE/R²), plus the signal Sharpe.
The pickled pipeline is stored as a SQLite BLOB.

| Capability | Purpose |
|---|---|
| `markets.ml.train` | Retrain a model. |
| `markets.ml.update` | Merge hyperparameter and feature changes, then retrain. This is the capability Vera uses to tune models. |
| `markets.ml.predict` | The live signal. |
| `markets.ml.series` | The full per-bar P(up) or forecast series, for charting. |
| `markets.ml.list` / `markets.ml.delete` | Manage models. |

### Strategies and backtesting

`markets.strategy.save` accepts five strategy kinds:

| Kind | Spec |
|---|---|
| **rule** | JSON DSL: `entry` conditions (ANDed) and `exit` conditions (ORed), each `{left, op, right}`. Operands are `close`, numbers, indicator specs (`{kind:'ema',params:{n:50}}`), custom indicators (`{kind:'cx_<id>', series}`), external datasets, or `{ml:'<model_id>'}` (P(up)). Ops are `> < >= <=`, `crosses_above` and `crosses_below`. Optional `short_entry` / `short_exit`, plus `fee_bps`, `slippage_bps`, `size_pct`, `stop_loss_pct`, `take_profit_pct`. |
| **ml** | `{ml_id, enter_above:0.6, exit_below:0.45}`, optionally `short_below` / `short_exit_above`. Trades a trained predictor's probability directly. |
| **fused** | `members` (saved strategy ids and/or inline specs, rule and ML mixed) combined with `combine: all\|any\|majority\|weighted` for entries and `exit_combine: any\|all`. This is how rule and AI strategies fuse into one signal. |
| **regime** | Multi-stage: different members arm in different market phases ([§5](#causal-pivots-and-regime-strategies)). |
| **dual** | One strategy on the long side and another on the short side, composed into a single long/short strategy. It cannot be nested. |

`_spec_signals` is the **single evaluator** shared by backtests and the live
monitor, so what you test is exactly what gets monitored.
`markets.backtest.signals` evaluates a spec's signals without a backtest, for
chart preview.

`markets.backtest.run` takes an `engine` parameter:

- **native** — vectorised, next-open fills, intrabar stop-loss and take-profit,
  long and short.
- **backtrader** — Cerebro plus the SQN and DrawDown analyzers. It is an optional
  dependency and long-only. `markets.backtest.engines` lists which engines are
  available.

The stats include:

- returns and risk: CAGR, Sharpe, Sortino, Calmar, maximum drawdown
- trade quality: win rate, profit factor, expectancy, average win/loss,
  best/worst trade
- exposure: average bars in trade, exposure
- SQN, on backtrader only

`markets.backtest.sweep` (plus `sweep_status`) grid-searches parameters in the
background and stores the winner as a backtest result. Results are managed with
`markets.backtest.list/get/delete`.

### Live monitoring and alerts

`markets.strategy.accept` puts a saved strategy under **live monitoring**:

- A **60-second** scheduler re-evaluates due monitors (per-strategy `interval_min`)
  on fresh bars from the configured dataset.
- It tracks a virtual position in `mkt_settings` (`mon:{id}`).
- It raises an alert on each **new** entry or exit signal. Alerts are stored in
  `mkt_alerts` and emitted as a `markets.alert` event, and can also be pushed to
  Telegram (channel `telegram`).
- A linked sim account can trade automatically (`sim_account_id` / `sim_pct`).

Related capabilities:

- `markets.monitor.status` is the monitoring dashboard.
- `markets.alerts.list/ack` manage the alert feed.
- `markets.strategy.archive` stops monitoring.

### Portfolio and UK CGT

`mkt_portfolio_tx` is a transaction ledger (buy/sell, quantity, price, fees)
across any symbol key, including custom assets. It is managed with
`markets.portfolio.tx_add/tx_list/tx_remove`.

`markets.portfolio.positions` does **FIFO lot accounting**. It returns quantity,
average cost and realised P&L, and values open positions from stored bars, so it
works offline and works for collectables. `markets.portfolio.history` replays the
ledger against daily closes to draw the value-against-cost curve.

`markets.tax.uk_cgt` estimates UK Capital Gains Tax on the ledger for a tax year
(6 April to 5 April). It applies the HMRC matching rules in order:

1. same-day
2. 30-day bed-and-breakfast
3. the Section 104 pool (buy fees are added to cost; sell fees are deducted from
   proceeds)

Inputs are `tax_year`, `annual_exempt` (default 3000), `rate_basic` (18),
`rate_higher` (24) and `basic_band_left`. The result is **an estimate, not tax
advice**.

---

## 4. Classic workbench panel

`markets_panel.html` (`/markets/panel`) is the original full-screen terminal. It
is no longer a tab of its own: it is served so that the studio can open its
node-graph **pipeline builder** in an overlay (`?pipe=1`), and its other unique
tools are folded into the studio (see [One panel](#one-panel)). Its layout:

- **Header** — unified asset search (crypto, stocks and collectables in one
  dropdown), live price and day change, sentiment chip, timeframe pills,
  track/refresh.
- **Left rail** — watchlist grouped by asset class with prices, change and
  sentiment dots; custom-asset creator; background job monitor.
- **Chart stack** — lightweight-charts candles and volume, overlay indicators,
  and one synced sub-pane per oscillator; panes share a logical range.
- **Drawing toolbar** — trendline, ray, hline, vline (key date), rect, fib and
  label, with select/move/delete. Shapes are drawn on a HiDPI overlay canvas mapped
  through fractional logical indices, so they survive pan and zoom.
- **Right dock** — Vera copilot (agent loop via `/workshop/agent_loop/stream`
  with a markets toolkit `allowed_caps`), Indicators, ML lab, Backtest,
  Sentiment heat-map, Portfolio.
- **Live updates** — WebSocket `/ws/mcp` and `subscribe_events`, with `/events`
  polling as a fallback. Panel-bridge action handlers (`chart_load`, `open_dock`,
  `reload_annotations`) make it drivable from the main chat.

---

## 5. Quant Studio

The **Quant Studio** (`markets_studio_capabilities.py` + `markets_studio_panel.html`,
served at `/markets/studio/panel`) is built around the backtester.

Its charts use a **custom canvas engine**, with no third-party charting library
and no watermarks. Features:

- animated candles and indicators, with a live price pulse
- crosshair-synced **tiling** in 1/2/3/4/6 grids
- per-tile indicator cards with inline parameter editing
- named **saved layouts** (`markets.layout.save/list/delete`)

**Strategy library and visual builder.**

- `markets.strategy.library` ships 23 curated templates across five categories
  (trend, momentum, mean-reversion, breakout, ml), with tuning hints.
- `markets.strategy.from_template` instantiates a template as a saved strategy.
  `overrides` sets any dotted spec path, so nobody has to edit JSON.
- The builder edits rule conditions as dropdown rows (price, indicator, ML model
  or number, against an operator).
- It also has ML-threshold and fusion editors, a signal preview onto the active
  chart, and an **ƒx indicator lab** for sandboxed custom-indicator expressions.

**Trend and regime fit.** `markets.analysis.trendfit` returns:

- an overall OLS fit with a ±2σ regression channel
- a piecewise fit whose segments sit at price **pivots** (Ramer–Douglas–Peucker
  on log price, with an OLS refit per segment), each labelled bull, bear or flat
  by its annualised slope

The 0–100 `detail` knob maps to the RDP ε, so one slider goes from a 2-segment
regime view to fine pivots.

**Backtest feedback, deep analytics and replay.**

- `markets.backtest.run` emits progress events for each run
  (`started → bars → signals → simulating → done`, all carrying the run id). They
  drive a live stage checklist in the Run Center.
- `markets.backtest.analyze` computes, from any stored run: the drawdown curve,
  longest time underwater, a monthly-return heat-map, rolling Sharpe, a trade
  histogram, streaks and the mix of exit reasons.
- With `replay=true`, it re-simulates at full resolution and returns per-bar
  equity, position and all trades. The UI uses this to **play the backtest
  through the chart**, with a playhead, speed control, camera-follow, trade
  markers and an equity sub-pane.

**Autotune.** `markets.backtest.autotune`:

1. auto-discovers the numeric paths in a spec
2. runs a multi-round zooming grid (coordinate descent beyond 3 axes; the span
   halves on improvement and widens when stuck)
3. persists the winner as an `[autotune]` backtest, and can write the parameters
   back to the strategy

Progress streams as `autotune_*` stages, and `markets.backtest.autotune_status`
polls it. The **agentic** counterpart is the `markets-quant` loop profile
(`vera/dag/loop_profiles.py`). It is limited to the library, backtest, autotune,
analyze and sim capabilities, with no real money.

**Baseline estate and Market Pulse.**

- `markets.baseline.list` and `markets.baseline.ensure` track a curated estate
  through the normal watchlist auto-updater: SPY/QQQ/DIA/IWM, all 11 SPDR sectors,
  ^TNX/^VIX/DXY/TLT, gold, oil, and BTC/ETH via Yahoo.
- `markets.overview` aggregates every watched asset into per-group stats:
  multi-window changes, RSI, 30-day volatility, 52-week range position, trend
  label, sparklines and breadth/medians. It has a 60-second cache.
- These feed the Pulse tab's animated sector **treemap**, breadth gauges, sector
  strips and per-asset drill-in infographics.

**Key dates.** `markets.events.detect/apply` covers:

- BTC/LTC halvings, including projected ones
- ETH upgrades
- market-wide shocks (COVID, FTX, elections, ETF approvals)
- for Yahoo assets, the IPO or first-trade date and dividends/splits from the
  chart API

These are written as deduplicated `vline` annotations (author `events`,
colour-coded), so they render on every chart.

**Macro and on-chain layers.** `markets.macro.catalog/fetch` covers:

- **FRED series**, via the keyless `fredgraph.csv` endpoint: fed funds, 2s/10s,
  10Y–2Y, CPI plus derived year-on-year inflation, unemployment, M2, and the Fed
  balance sheet.
- **blockchain.info charts**: hash rate, transactions, active addresses, BTC
  supply, miner revenue and market cap.

These land as ordinary `mkt.macro.<slug>.1d` datasets. A `macro` ingestor is
registered into `PROVIDER_INGESTORS`, so the series ride the shared job runner
and auto-refresh through the watchlist. Any chart tile can layer up to four of
them on a secondary scale through the ⧉ menu.

**Sim accounts (paper trading).** `markets.sim.create/list/order/equity/reset/delete`:

- Accounts have cash and a fee in basis points.
- Market orders fill at the latest stored price, sized by quantity, notional, or
  % of cash/position.
- Equity is snapshotted hourly and on every order.
- `markets.sim.templates` offers one-click profiles: Balanced 60/40, All-Weather,
  Crypto Degen, Sector Rotator and Strategy Sandbox.
- The strategy monitor auto-trades linked accounts (entry buys `sim_pct`% of
  cash; exit closes). This is the safe environment for agent loops to trade in.

**ML integration.** `markets.ml.series` returns the full per-bar P(up) or forecast
series, so trained models plot as chart indicators and slot into the visual
builder as first-class operands.

### One panel

The **Markets tab is the Quant Studio**, with single left-rail navigation and no
sub-tab bar.

The classic panel is not navigable; it is served at `/markets/panel` only so that
the studio's ⛓ button can open the node-graph **pipeline builder in an overlay**
(`?pipe=1` opens it automatically). Its other unique tools are folded in:

- **ledger** — the Project view adds and deletes real portfolio transactions
- **custom assets** — in the data drawer

The chart-workspace controls (grid, tiles, sync, animations, watch strip, data
and layers) live in a **⚙ workspace popover** on the top app bar.

The studio's **copilot dock** has two modes:

- **⟳ loop** runs the agent loop as the selected specialist (`quant-strategist`,
  `market-visualizer` or `indicator-smith`), with the `markets-quant` profile and
  the `sys-quant-visuals` guideline skill attached.
- **💬 chat** streams `/agents/chat/stream`. That endpoint only *lists* tool
  signatures, so the studio runs a **client-side tool loop**:
  1. The agent's `{"tool_use":…}` action is parsed.
  2. It is executed for real: through `/mcp/call` for `markets.*` and
     `web.search`, or as a local **UI tool** (`ui.chart_load`, `ui.switch_view`,
     `ui.overlay_strategy`, `ui.pin_infographic`, `ui.open_result`).
  3. The `[tool_result]` is fed back, for up to 7 rounds.

The same UI tools are registered on the **panel bridge**, so any agent can drive
the studio through `panel.dispatch`. `markets.specialist_context` gives the
specialist a compact live snapshot (market breadth, open positions and so on) to
reason from. The self-improve loop ([§7](#7-self-improvement-loop)) is surfaced
in the Run Center.

### Leverage, forward-testing and multi-strategy accounts

- **Leverage.** Specs take `leverage` on the native engine, up to 10×.
  - Notional multiplies, cash goes negative (borrowed), and equity is marked to
    market.
  - **Equity ≤ 0 liquidates** the position (flat, with a `liquidated` stat).
  - Leverage is excluded from autotune axes. The builder has the field, and
    batch screening includes it.
- **Forward-test** on any backtest result puts the strategy live on paper. It
  saves the strategy if it was ad-hoc, accepts it for monitoring on its dataset,
  and links the shared `forward-tests` sim account. The Run Center's **Live
  paper runs** card lists monitors (position, last signal, sim link) with
  one-click stop.
- **Many strategies at once.** The **monitor launcher** (bell menu, Strategy
  view, or "＋ monitor…" on the paper-runs card) multi-selects strategies and
  starts a monitor for each, with per-monitor interval, channels (in-app, plus
  optional Telegram) and an optional shared sim account where each trades its own
  sleeve. The topbar **bell** shows the unseen-alert count live, and lists and
  acknowledges every monitor's signals.
- **Sim sleeves.** Sim orders record their `source` (`strategy:<id>`, user,
  template or optimizer). Exits triggered by a monitor sell only that source's
  sleeve, so several strategies, or one strategy on several timeframes, can share
  ONE account without liquidating each other. Positions expose the per-source
  breakdown (`sleeves`).
- **Metrics are separated from the market.** `markets.overview` skips `macro` and
  `dyn` watchlist rows, so they never enter the Pulse market map. They render in
  their own **Metrics rail** instead (spark cards; a click opens the metric as its
  own chart tile).

### Shorting

The native engine simulates **long and short** (`run_backtest(arr, entry, exit_,
opts, short_entry, short_exit)`):

- Short-sale proceeds are credited on entry, and the liability is marked to market.
- Stop-loss and take-profit are inverted for shorts.
- **Flips:** an opposing entry signal closes the position and reverses it at the
  same open, with exit reason `flip`. Cross signals fire on a single bar, so
  without flips an always-in strategy could never enter its short leg.

In the DSL, rule specs take `short_entry`/`short_exit` condition lists (long-only,
short-only, or both), and ml specs take `short_below` / `short_exit_above` on
P(up). Stats gain `long_trades`/`short_trades`, and trades carry `side`.

The builder has ▲ long and ▼ short sections. The templates `supertrend-long-short`
and `rsi-fade-short` ship in the library. The backtrader engine remains long-only,
and is guarded accordingly.

### Pivot points (pluggable)

`markets.analysis.pivots` has one output shape (`pivots:[{t,p,kind}]` plus a line)
across four methods:

| Method | Detects pivots by |
|---|---|
| `zigzag` | % reversal |
| `atr_zigzag` | a multiple of ATR (volatility-adaptive) |
| `fractal` | Williams fractals |
| `rdp` | path simplification, using the trendfit `detail` knob |

New detectors therefore drop into the same chart menu and chart layer (a swing
polyline with high/low diamonds).

### Projections, optimizer and rotation

- **`markets.project.asset` / `markets.project.portfolio`** produce Monte-Carlo
  GBM bands (p10…p90).
  - The drift and volatility come from history, **or from a strategy's backtested
    equity curve** (`strategy_map` / `strategy_backtest_id`, in which case fees are
    already included).
  - Bands are summed to portfolio level, in **nominal and real terms**. Inflation
    is read automatically from the fetched `fred:CPI_YOY` layer, and can be
    overridden with a local rate.
  - An optional annual cost drag can be applied.
  - The ⧗ Project view renders the fan chart and per-asset assumptions.
- **`markets.portfolio.optimize`** builds a Monte-Carlo efficient frontier over
  aligned daily returns (correlations included).
  - It returns max-Sharpe, max-return and min-volatility weights against current
    holdings, and a fee-priced rebalance plan (turnover × fee bps).
  - `apply='sim:<id>'` executes the plan as paper orders.
- **`markets.rotation.scan`** is the BTC→ETH "optimal path" scanner.
  - Every asset is scored by blended momentum z-scores, trend regime, live
    accepted-strategy signals and, optionally, ML P(up).
  - Held laggards get fee-aware switch suggestions, with the pair-ratio spark.

### Causal pivots and regime strategies

- **Causal pivot indicators.** `pivotlevels` gives the last *confirmed* pivot
  high/low as step levels, and `pivotdir` gives the current confirmed leg (±1).
  Both are **causal**: they are verified not to repaint on truncation. Tuned
  pivots are therefore first-class, sweepable strategy operands. The library
  templates are `pivot-breakout` (both sides) and `pivot-trend`.
- **Regime strategies.** Strategy kind `regime` runs phased, multi-stage
  backtests with a spec of the form `{regimes:[{when:'bull|bear|flat|any',
  strategy_id|spec}], regime_source:{method:'sma|supertrend|pivots'},
  exit_on_regime_change}`.
  - Member entries only arm in their phase. Exits always work.
  - A phase flip closes positions by default.
  - The builder has a regime editor, and the `regime-switcher` template ships
    (bull → EMA trend, bear → RSI short-fade).

### Deeper autotune

`markets.backtest.autotune` guards against over-fitting:

- **Out-of-sample holdout.** It holds back a tail (`oos_split=0.25`) that the
  search never sees. The winner is re-scored on it (`stats_oos`), and the UI shows
  an "overfit risk" or "holds up" verdict.
- **Validation re-selection.** The top 8 in-sample finalists are re-scored on a
  held-back validation slice, and the validation winner ships (`validation_pick`).
- **Minimum trades.** Combinations under `min_trades` are rejected.
- **Exploration.** Random jitter is added each round to escape local grids.
- **Sensitivity sweep.** A final sweep varies each winning parameter across ±40%
  and renders the result as robustness bars.

Other options:

- `metric='blend'` is a Sharpe-led composite with Calmar and profit factor.
- Budgets go up to 10 rounds × 400 evaluations.

### Live strategy overlay

Every chart tile has a strategy picker. Choose any saved strategy and its entry,
exit and short signals render as live markers on the price action. A pulsing
"▲ LONG NOW / ▼ SHORT NOW" badge appears when a signal fires on the latest bar,
and the markers refresh with each bar update.

### Market dynamics, news and OSINT

- **Positioning: `markets.dynamics.fetch/snapshot`.** Open longs against shorts,
  from keyless Binance-futures data: funding rate, open interest, the global
  long/short account ratio and the top-trader position ratio.
  - Fetched series land as `mkt.dyn.<pair>_<metric>.1d` datasets, through a `dyn`
    provider ingestor that auto-refreshes via the watchlist.
  - The snapshot powers the live Positioning card in Pulse: funding, long%/short%
    bars and a crowding note.
- **Retail chatter: `markets.wsb.scan`.** It collects hot posts from retail
  subreddits and counts ticker mentions (watchlist symbols plus a known set,
  noise-filtered), weighted by upvotes. The top tickers are appended to daily
  `mkt.dyn.wsb_<ticker>.1d` series.
- **News.**
  - `markets.news.feed` returns per-asset or market headlines, cached for the
    Pulse dashboard. `map_to_chart` pins the top headline.
  - `markets.news.sources/subscribe/unsubscribe` manage saved-search news sources,
    optionally restricted to a domain.
  - `markets.news.digest` aggregates fresh headlines across all of them.
- **Backtest integration.** The rule DSL accepts external-series operands, such as
  `{dataset:'mkt.dyn.btc_usdt_funding.1d'}` or a bare `'mkt.…'` string. The series
  is forward-filled onto the strategy's bars, so funding, open interest,
  positioning, WSB, sentiment and macro series all become first-class entry and
  exit conditions.
  - Chart tiles can layer the same datasets through ⧉.
  - Metrics (hash rate, shorts, funding and so on) are searchable from the top
    search bar and open as their own chart tiles.

### Compound strategies, versioning and the trader director

- **Weighted compounds.** `kind='fused'` supports `combine:'weighted'` with
  `weights:[…]` and `enter_threshold`/`exit_threshold`.
  - Members vote with weights, and the composite fires on the normalised score.
  - Weights and thresholds are ordinary numeric spec leaves, so autotune and
    sweeps optimise the composition itself.
  - Monitors and alerts evaluate the same spec.
- **Never lose a setup.** Every strategy overwrite (autotune adopt, optimise
  re-run, manual edit, evolve promotion) automatically snapshots the previous spec
  (`markets.strategy.versions` / `.revert`, the ⟲ button in the builder). Reverts
  are themselves snapshotted.
- **Backtest windows.** The run form takes friendly windows (all history, 5y,
  1y and so on, or custom dates) instead of bar counts. Every result gets a
  full-history price strip whose edges you can **drag** to re-run the backtest
  over any sub-window live (`markets.backtest.run save=false`, so results are not
  stored).
- **Trader director** (`markets.trader.status/config.set/tick`). This is a
  scheduled, deterministic loop that:
  1. monitors the market
  2. rolls a strategies × assets **results grid** in the background: a few
     backtested cells per tick, plus a weighted composite of the top two
     strategies per asset
  3. executes fresh signals from cells above `min_metric`

  When it is enabled, the `mkt_trader` schedule checks every 60 seconds and runs
  a tick once `interval_min` has passed since the last one (default 60 minutes,
  minimum 5).

  It has two modes:
  - **Sim mode** trades only its configured sim account, with per-strategy
    sleeves and a maximum-positions cap. It cannot see the real book.
  - **Real mode** never touches sim accounts, and only raises alerts unless
    `real_autolog` opts into ledger records.

  Optional **LLM steering** adjusts `per_trade_pct` (5–50), `min_metric` (0–3)
  and `max_positions` (1–12) every N ticks, within those hard bounds. It never
  places trades, and only sees the active mode's account. A Run Center card
  drives the director.
- **Workspaces and watchlist.**
  - The topbar workspace switcher loads saved layouts or **account-bound
    workspaces**. Pick a sim account or the real portfolio, and its bound layout
    loads, or one is generated from its holdings.
  - The watchlist manager (in the ⚙ workspace popover) lists every tracked asset
    with per-asset auto-update toggles, untrack, and full-history fetch. One click
    fetches the **maximum available history on daily and weekly** for the whole
    watchlist.

### Screener, ML walk-forward, infographics and agents

- **`markets.backtest.batch`** (plus `_status`) backtests and ranks every
  strategy × dataset combination.
  - `'library'` screens all templates, and `all_watchlist` covers every tracked asset.
  - `autotune_top` optionally fine-tunes the N best with a 2-round zoom grid.
  - The leaderboard persists (`studio:batch:last`), and the Run Center renders it
    as the best-plays table.
- **`markets.ml.walkforward`** is honest out-of-sample ML backtesting.
  - It retrains on an expanding window per fold, and predicts only on unseen bars.
  - The stitched out-of-sample signal is traded through the native engine (short
    side optional).
  - The result lands as a normal backtest row (engine `ml-walkforward`).
- **`markets.infographic.save/list/delete`** lets agents compose live infographics
  (panel types stat, spark, bars, donut, gauge, heatmap and text) that render
  instantly in the Pulse tab.
- **Specialist agents** (`vera/agents/agents.py`): **quant-strategist** (drives the
  `markets-quant` loop profile), **indicator-smith** and **market-visualizer**.

---

## 6. Brokers and real accounts

`markets.broker.*` links **real** exchange and broker accounts. The providers
(`markets.broker.providers`) are:

| Provider id | Credential fields |
|---|---|
| `ccxt` (crypto exchange) | `api_key`, `api_secret`, `password` (some exchanges) |
| `t212-live`, `t212-demo` (Trading 212) | `api_key` |
| `alpaca-paper`, `alpaca-live` (Alpaca) | `api_key`, `api_secret` |

| Capability | Purpose |
|---|---|
| `markets.broker.link` | Link an account. The keys are **sealed at rest** with `security/secrets.py` ([29](./29-security.md)) and never returned. A balance read verifies the keys before saving. The account is linked **read-only** (`can_trade = 0`). |
| `markets.broker.list` | Linked accounts (`id`, exchange, label, `can_trade`); never keys. |
| `markets.broker.balances` | Read live balances (a read-only call). |
| `markets.broker.set_trading` | Enable or disable real order placement on one account. It is off by default. |
| `markets.broker.order` | Place a **real** order: `id`, `symbol`, `side` (buy/sell), `type` (market/limit), `amount` (base units), `price` (required for limit), `confirm`. It is refused unless trading is enabled **and** `confirm=true`. It emits a `markets.broker` event and sends a Telegram notice when Telegram is loaded. |
| `markets.broker.unlink` | Remove an account and its sealed keys. |

Linked accounts are stored in the `mkt_broker` table.

---

## 7. Self-improvement loop

`markets_evolve_capabilities.py` runs a perpetual optimisation loop over two fronts.

**Strategies.** For each target (a saved strategy plus a dataset), it:

1. derives a parameter grid from the strategy's own spec
2. runs the grid through `markets.backtest.sweep`
3. when the best result beats both the incumbent and the acceptance floor, writes
   the parameters back (`markets.strategy.save`) and puts the strategy live
   (`markets.strategy.accept`)

Persistent underperformers are archived. Each iteration re-centres the grid on
the current best, and widens the search when a target stops improving.

**Its own agent loop.** Every N ticks it starts a Loop Lab improve session
(`evolve.improve.start`), scoped to markets benchmark tasks
([33](./33-evolve.md)).

| Capability | Purpose |
|---|---|
| `markets.evolve.tick` | Run one iteration |
| `markets.evolve.start` / `markets.evolve.stop` | Start or stop the loop (ticks every `interval_minutes`) |
| `markets.evolve.status` / `markets.evolve.history` | Config, live flag, leaderboard and recent iterations |
| `markets.evolve.config.set` | `interval_minutes`, `metric`, `min_metric`, `grid_steps`, `grid_span`, `max_axes`, `auto_accept`, `archive_floor`, `improve_agent_loop`, `improve_every_ticks`, `sweep_timeout_s`, `targets` |
| `markets.evolve.prune_backtests` | Keep only the best N evolve-created backtests per strategy. It never removes hand-made backtests. |

The loop's state lives in Redis under `vera:markets:evolve:config`, `:state`,
`:leaderboard` and `:history`.

---

## 8. Capability index

| Group | Capabilities |
|---|---|
| Ingestion and watchlist | `markets.exchanges`, `markets.timeframes`, `markets.symbols`, `markets.fetch`, `markets.jobs`, `markets.update_now`, `markets.watchlist.list/add/config/remove` |
| Data | `markets.lookup`, `markets.asset.add`, `markets.bars`, `markets.quotes`, `markets.custom.create/add_price/import_csv/list/delete`, `markets.live.set/ticks`, `markets.history.audit/repair` |
| Indicators and annotations | `markets.indicators`, `markets.indicator_config.get/set`, `markets.indicator.custom.save/list/delete/test`, `markets.annotate.add/list/update/remove` |
| Sentiment and news | `markets.sentiment.analyze/map/refresh/history/to_series`, `markets.news.feed/sources/subscribe/unsubscribe/digest`, `markets.wsb.scan`, `markets.dynamics.fetch/snapshot` |
| ML | `markets.ml.create/list/update/train/predict/series/delete`, `markets.ml.walkforward` |
| Strategies | `markets.strategy.save/list/delete/accept/archive/library/from_template/versions/revert` |
| Backtesting | `markets.backtest.engines/run/list/get/delete/signals/sweep/sweep_status/analyze/autotune/autotune_status/batch/batch_status` |
| Analysis | `markets.analysis.trendfit`, `markets.analysis.pivots`, `markets.overview`, `markets.baseline.list/ensure`, `markets.events.detect/apply`, `markets.macro.catalog/fetch`, `markets.rotation.scan` |
| Monitoring | `markets.monitor.status`, `markets.alerts.list/ack` |
| Portfolio and tax | `markets.portfolio.tx_add/tx_list/tx_remove/positions/history/optimize`, `markets.project.asset/portfolio`, `markets.tax.uk_cgt` |
| Paper trading | `markets.sim.templates/create/list/order/equity/reset/delete`, `markets.trader.status/config.set/tick` |
| Real accounts | `markets.broker.providers/link/list/balances/set_trading/order/unlink` |
| Studio and agents | `markets.layout.save/list/delete`, `markets.infographic.save/list/delete`, `markets.specialist_context` |
| Self-improvement | `markets.evolve.tick/start/stop/status/history/config.set/prune_backtests` |

---

## 9. Storage, schedules and events

**SQLite tables** (the shared Data-Fabric database):

| Table | Contents |
|---|---|
| `mkt_watchlist` | Tracked assets and their auto-update settings |
| `mkt_custom_assets` | Custom series |
| `mkt_live_ticks` | Live ticks |
| `mkt_settings` | KV store: indicator configs, monitor positions, studio state |
| `mkt_annotations` | Chart drawings |
| `mkt_custom_indicators` | Custom indicator expressions |
| `mkt_sentiment` | Sentiment snapshots |
| `mkt_ml_models` | ML models |
| `mkt_strategies` | Saved strategies |
| `mkt_backtests` | Backtest results |
| `mkt_alerts` | Monitor alerts |
| `mkt_portfolio_tx` | Portfolio ledger |
| `mkt_sim_accounts`, `mkt_sim_orders`, `mkt_sim_equity` | Paper trading |
| `mkt_broker` | Linked broker accounts (sealed keys) |

Bars live in `fabric_records` under `mkt.*` datasets ([06](./06-data-fabric.md)).

**Schedules:**

| Name | Interval | Job |
|---|---|---|
| `mkt_autoupdate` | 60 s | Watchlist refreshes that are due |
| `mkt_live_tick` | 20 s | Live tick recording |
| `mkt_strategy_monitor` | 60 s | Strategy monitors |
| `mkt_sim_snapshot` | 1 h | Sim equity snapshots |
| `mkt_trader` | 60 s | Trader director, when it is enabled and its `interval_min` (default 60) has elapsed |
| `markets_evolve_startup` | once | Seeds the markets benchmark tasks into Loop Lab and resumes the self-improve loop if it was enabled |

**Events.** These refresh open panels:

- `markets.fetch`, `markets.tick`, `markets.watchlist`
- `markets.indicators`, `markets.annotate`, `markets.sentiment`
- `markets.ml`, `markets.backtest`, `markets.strategy`
- `markets.monitor`, `markets.alert`, `markets.portfolio`
- `markets.sim`, `markets.trader`, `markets.broker`, `markets.baseline`
- `markets.events`, `markets.macro`, `markets.osint`, `markets.infographic`
- `markets.evolve*`

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ccxt not installed` | Install `ccxt` for crypto, or use Yahoo and custom assets only. |
| Yahoo daily history looks quarterly | Use the provider (explicit `period1`/`period2`) rather than `range=max`. |
| Intraday history stops short | Yahoo's intraday windows are capped (1m → 7d, 5–30m → 59d, 1h → 729d). |
| Gaps in a series | Run `markets.history.audit`, then `markets.history.repair`. |
| `backtrader` engine missing | It is optional. `markets.backtest.engines` shows availability. Use `native` for shorts and leverage. |
| Sentiment returns *"no headlines found"* | `web.search` is unavailable or returned nothing. |
| `markets.broker.order` refused | Enable trading with `markets.broker.set_trading` and pass `confirm=true`. |
| The trader director never acts | Check `markets.trader.status`: is it enabled, has `interval_min` elapsed, and is a sim account set for sim mode? |

---

## See also

- [Data Fabric](./06-data-fabric.md) — where bars are stored (`mkt.*` datasets) and the single-writer queue
- [Machine Learning](./16-machine-learning.md) — the general ML workshop; the markets ML tools are self-contained but share the fabric
- [Agents & Chat](./19-agents-chat.md) — the agent-loop stream the embedded copilot uses
- [Capability Framework](./01-capability-framework.md) — `markets.*` registration and events
- [Evolve / Loop Lab](./33-evolve.md) — improve sessions the self-improve loop starts
- [Security & Secrets](./29-security.md) — how broker keys are sealed
- [Business & Commerce](./37-business-commerce.md) — the reselling operation, which shares the Fabric database

## Screenshots (operator-captured)

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
