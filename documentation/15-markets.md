# 15 · Markets

`markets/markets_capabilities.py` pulls full **OHLCV** (open/high/low/close/volume) history for selected crypto assets from any **CCXT** exchange, stores every bar in the [Data Fabric](./06-data-fabric.md), keeps a persistent **watchlist**, and refreshes it on a schedule. It's the canonical example of a high-volume numeric ingestion source that lands in the fabric without going through LLM enrichment.

`ccxt` is an optional dependency (`HAS_CCXT`) — absent, the module loads but its caps report unavailable rather than breaking startup.

---

## 1. Design notes

- **CCXT is driven synchronously inside `loop.run_in_executor`** so it never blocks the event loop, and Vera stays version-agnostic across `ccxt` 1.x → 4.x (the unified `load_markets` / `fetch_ohlcv` API is stable).
- **Every fetch is a background job.** It returns a `job_id` immediately and streams progress — this is what removes the old 30-second HTTP timeout on a multi-year backfill.
- **One dataset per asset+timeframe:** `mkt.{exchange}.{base}_{quote}.{tf}`. Each bar is a `fabric_records` row with a *deterministic* id `f"{dataset_id}:{ts_ms}"`, so re-ingesting hits `INSERT OR REPLACE` and dedupes automatically — incremental updates never duplicate a bar.
- Bars are written through the fabric **single-writer queue** (`_enqueue_write`), deliberately bypassing `ingest_dataset` so the per-ingest LLM entity-extraction / auto-tagging never fires on numeric bar data.

### Defaults

| Constant | Value |
|---|---|
| Exchanges | `binance`, `coinbase`, `kraken`, `bybit` |
| Default timeframes | `1d`, `1h` |
| All timeframes | `1m 5m 15m 30m 1h 4h 1d 1w` |
| Full-backfill start | `2013-01-01` (exchanges that only serve recent data return what they have) |

---

## 2. Capabilities

### Discovery

| Cap | Purpose |
|---|---|
| `markets.exchanges` | List supported/configured CCXT exchanges |
| `markets.timeframes` | List available timeframes |
| `markets.symbols` | List tradeable symbols on an exchange (cached hourly per `_MARKETS_TTL`) |

### Ingestion

| Cap | Purpose |
|---|---|
| `markets.fetch` | Start a background OHLCV backfill/update job → returns `job_id` |
| `markets.jobs` | Poll in-flight and recent fetch jobs (progress, bar counts) |
| `markets.update_now` | Force an immediate refresh of the watchlist |

### Watchlist

A persisted set of asset+timeframe targets refreshed on the scheduler:

| Cap | Purpose |
|---|---|
| `markets.watchlist.list` | Current watchlist entries |
| `markets.watchlist.add` | Add an exchange/symbol/timeframe target |
| `markets.watchlist.config` | Tune refresh cadence / settings |
| `markets.watchlist.remove` | Remove a target |

---

## 3. Querying the data

Because bars are ordinary fabric records, anything that reads the fabric reads market data. Query a dataset directly:

```json
POST /fabric/query
{ "dataset": "mkt.binance.BTC_USDT.1d", "limit": 365, "order": "ts_ms desc" }
```

Or pull it into the [Machine Learning](./16-machine-learning.md) module — `ml.data.fetch_ohlcv` and the fabric loaders turn these datasets straight into NumPy training arrays.

---

## 4. UI

**`markets-panel`** (`markets_panel.html`) is the watchlist manager and ingestion console: pick an exchange/symbol/timeframe, kick off a backfill, watch job progress, and chart stored bars. Safety caps (`_MAX_BARS_PER_TF`, `_MAX_JOBS`) bound a single backfill and the in-flight job table.

---

## See also

- [Data Fabric](./06-data-fabric.md) — where bars are stored (`mkt.*` datasets) and the single-writer queue
- [Machine Learning](./16-machine-learning.md) — consumes `mkt.*` / OHLCV datasets for training
- [Device Mesh](./14-mesh.md) — sibling module sharing the background-job + WAL-SQLite pattern
- [Capability Framework](./01-capability-framework.md) — `markets.*` registration & events
