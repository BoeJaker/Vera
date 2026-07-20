"""
markets_studio_capabilities.py — Quant Studio backend
=====================================================

The "fully-featured backtester" layer that powers the Quant Studio panel:

• **Strategy library** (`markets.strategy.library` / `.from_template`) — a
  curated set of classic, ready-to-run strategies (trend / momentum /
  mean-reversion / breakout / volatility / ml) expressed in the existing rule
  DSL. The UI instantiates them without anyone touching JSON.

• **Trend & regime fit** (`markets.analysis.trendfit`) — overall least-squares
  fit (+ regression channel) plus a multi-segment piecewise fit whose segment
  count follows a 0–100 `detail` knob. Segments are placed at *pivots* via
  Ramer–Douglas–Peucker on the (log-)price path, then each segment is OLS
  re-fitted and labelled bull / bear / flat by annualised slope.

• **Backtest deep analytics & replay** (`markets.backtest.analyze`) —
  drawdown curve, underwater periods, monthly-return heat-map, rolling Sharpe,
  trade-return histogram, streaks, exit-reason mix … and an optional full-
  resolution `replay` payload (per-bar equity + position + full trade list) so
  the UI can *play the backtest through the chart* bar by bar.

• **Deterministic autotune** (`markets.backtest.autotune`) — multi-round
  zooming grid search: numeric spec paths are auto-discovered, a coarse grid is
  evaluated, the grid re-centres on the winner and shrinks (or widens when
  stuck), for N rounds. Winner is persisted as a normal backtest row and can be
  written back to the strategy. Progress streams as `markets.backtest` events
  (stages `autotune_*`). The agentic counterpart is the `markets-quant` loop
  profile.

• **Baseline market monitor** (`markets.baseline.ensure` / `markets.overview`)
  — one call tracks a curated estate (indices, all 11 SPDR sectors, rates, FX,
  commodities, BTC/ETH) via the normal watchlist auto-updater; `markets.overview`
  aggregates every tracked asset into per-sector / whole-market stats
  (multi-window changes, RSI, vol, 52-week range position, trend label,
  sparklines) that feed the live infographics.

• **Key dates** (`markets.events.detect` / `.apply`) — halvings, network
  upgrades, market-wide shocks, IPO/first-trade, dividends & splits (yahoo) are
  detected per asset and written as `vline` annotations (author `events`) so
  every chart shows them.

• **Macro / on-chain compare layers** (`markets.macro.catalog` / `.fetch`) —
  FRED series (rates, CPI + derived YoY inflation, unemployment, M2, Fed
  balance sheet), treasury-yield tickers and blockchain.info charts (hash rate,
  tx count, addresses, supply) land as ordinary `mkt.macro.*.1d` datasets that
  any chart can layer on a secondary scale. A `macro` provider ingestor is
  registered into the shared job runner so these series auto-refresh through
  the same watchlist scheduler as price data.

• **Sim accounts** (`markets.sim.*`) — paper-trading accounts (cash, orders
  filled at latest stored price, fees), equity snapshots, and a hook the
  strategy monitor uses to auto-trade accepted strategies into a sim account —
  the agent-safe trading environment.

• **Saved layouts** (`markets.layout.*`) — named chart/tile layouts for the
  studio UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.markets.studio")

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:                              # pragma: no cover
    HAS_NUMPY = False

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, enum_schema, now_iso, ollama_generate,
        register_ui, schedule,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.markets.studio").warning(
        "markets studio caps unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Strategy library (pure data — instantiated through markets.strategy.save)
# ─────────────────────────────────────────────────────────────────────────────
#
# Every template is a plain rule-DSL spec (see markets_lab_capabilities) plus
# UI metadata and `tune` axes that seed sweeps / autotune. Operands reference
# indicator kinds computed by markets_analysis_capabilities — including the
# studio additions (donchian, supertrend, keltner, adx, psar, ichimoku, …).

_COSTS = {"fee_bps": 10, "slippage_bps": 5}

STRATEGY_LIBRARY: List[dict] = [
    {
        "id": "sma-golden-cross", "name": "Golden Cross", "category": "trend",
        "icon": "✚", "difficulty": "starter",
        "description": "Buy when the 50-bar SMA crosses above the 200-bar SMA, "
                       "exit on the death cross. The classic slow trend filter.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "sma", "params": {"n": 50}},
                            "op": "crosses_above",
                            "right": {"kind": "sma", "params": {"n": 200}}}],
                 "exit":  [{"left": {"kind": "sma", "params": {"n": 50}},
                            "op": "crosses_below",
                            "right": {"kind": "sma", "params": {"n": 200}}}]},
        "tune": [{"path": "entry.0.left.params.n",  "label": "fast SMA", "from": 20, "to": 90, "step": 10},
                 {"path": "entry.0.right.params.n", "label": "slow SMA", "from": 120, "to": 280, "step": 40}],
    },
    {
        "id": "ema-trend-rider", "name": "EMA Trend Rider", "category": "trend",
        "icon": "⤴", "difficulty": "starter",
        "description": "Ride the 20/50 EMA cross with an 8% stop — quicker than "
                       "the golden cross, more trades, tighter risk.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 8,
                 "entry": [{"left": {"kind": "ema", "params": {"n": 20}},
                            "op": "crosses_above",
                            "right": {"kind": "ema", "params": {"n": 50}}}],
                 "exit":  [{"left": {"kind": "ema", "params": {"n": 20}},
                            "op": "crosses_below",
                            "right": {"kind": "ema", "params": {"n": 50}}}]},
        "tune": [{"path": "entry.0.left.params.n",  "label": "fast EMA", "from": 8, "to": 34, "step": 4},
                 {"path": "entry.0.right.params.n", "label": "slow EMA", "from": 40, "to": 100, "step": 10},
                 {"path": "stop_loss_pct", "label": "stop %", "from": 4, "to": 14, "step": 2}],
    },
    {
        "id": "rsi-mean-reversion", "name": "RSI Snapback", "category": "mean-reversion",
        "icon": "↩", "difficulty": "starter",
        "description": "Buy oversold (RSI < 30), sell the bounce (RSI > 55). "
                       "Works best on choppy, range-bound assets.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 12,
                 "entry": [{"left": {"kind": "rsi", "params": {"n": 14}},
                            "op": "<", "right": {"value": 30}}],
                 "exit":  [{"left": {"kind": "rsi", "params": {"n": 14}},
                            "op": ">", "right": {"value": 55}}]},
        "tune": [{"path": "entry.0.right.value", "label": "buy below", "from": 20, "to": 40, "step": 5},
                 {"path": "exit.0.right.value",  "label": "sell above", "from": 50, "to": 75, "step": 5},
                 {"path": "entry.0.left.params.n", "label": "RSI period", "from": 7, "to": 21, "step": 7}],
    },
    {
        "id": "rsi-momentum", "name": "RSI Momentum Gate", "category": "momentum",
        "icon": "⚡", "difficulty": "starter",
        "description": "Enter when RSI pushes up through 55 while price holds "
                       "above the 200-bar EMA; exit when momentum fades below 45.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "rsi", "params": {"n": 14}},
                            "op": "crosses_above", "right": {"value": 55}},
                           {"left": "close", "op": ">",
                            "right": {"kind": "ema", "params": {"n": 200}}}],
                 "exit":  [{"left": {"kind": "rsi", "params": {"n": 14}},
                            "op": "<", "right": {"value": 45}}]},
        "tune": [{"path": "entry.0.right.value", "label": "entry level", "from": 50, "to": 65, "step": 5},
                 {"path": "exit.0.right.value",  "label": "exit level", "from": 35, "to": 50, "step": 5}],
    },
    {
        "id": "macd-cross", "name": "MACD Cross", "category": "momentum",
        "icon": "〰", "difficulty": "starter",
        "description": "Buy the MACD line crossing above its signal, exit on "
                       "the cross back below. The workhorse momentum trigger.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "macd", "series": "macd"},
                            "op": "crosses_above",
                            "right": {"kind": "macd", "series": "macd_signal"}}],
                 "exit":  [{"left": {"kind": "macd", "series": "macd"},
                            "op": "crosses_below",
                            "right": {"kind": "macd", "series": "macd_signal"}}]},
        "tune": [{"path": "entry.0.left.params.fast", "label": "fast", "from": 8, "to": 16, "step": 2},
                 {"path": "entry.0.left.params.slow", "label": "slow", "from": 20, "to": 34, "step": 4}],
    },
    {
        "id": "adx-filtered-macd", "name": "MACD + ADX Filter", "category": "trend",
        "icon": "▲", "difficulty": "intermediate",
        "description": "MACD cross entries, but only when ADX says a real trend "
                       "is underway (ADX > 20) — filters the chop.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "macd", "series": "macd"},
                            "op": "crosses_above",
                            "right": {"kind": "macd", "series": "macd_signal"}},
                           {"left": {"kind": "adx", "series": "adx"},
                            "op": ">", "right": {"value": 20}}],
                 "exit":  [{"left": {"kind": "macd", "series": "macd"},
                            "op": "crosses_below",
                            "right": {"kind": "macd", "series": "macd_signal"}}]},
        "tune": [{"path": "entry.1.right.value", "label": "ADX floor", "from": 15, "to": 35, "step": 5}],
    },
    {
        "id": "bollinger-snapback", "name": "Bollinger Snapback", "category": "mean-reversion",
        "icon": "◡", "difficulty": "starter",
        "description": "Buy a close below the lower band, exit at the middle "
                       "band. Mean reversion with a volatility-aware entry.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 10,
                 "entry": [{"left": "close", "op": "<",
                            "right": {"kind": "bbands", "series": "bb_lower"}}],
                 "exit":  [{"left": "close", "op": ">",
                            "right": {"kind": "bbands", "series": "bb_mid"}}]},
        "tune": [{"path": "entry.0.right.params.n", "label": "band period", "from": 14, "to": 30, "step": 4},
                 {"path": "entry.0.right.params.k", "label": "band width", "values": [1.5, 2.0, 2.5, 3.0]}],
    },
    {
        "id": "bollinger-breakout", "name": "Bollinger Breakout", "category": "breakout",
        "icon": "◠", "difficulty": "starter",
        "description": "Momentum flavour of the bands: buy strength through the "
                       "upper band, exit when price loses the middle band.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "bbands", "series": "bb_upper"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "bbands", "series": "bb_mid"}}]},
        "tune": [{"path": "entry.0.right.params.n", "label": "band period", "from": 14, "to": 30, "step": 4},
                 {"path": "entry.0.right.params.k", "label": "band width", "values": [1.5, 2.0, 2.5]}],
    },
    {
        "id": "donchian-breakout", "name": "Donchian Breakout", "category": "breakout",
        "icon": "⊓", "difficulty": "intermediate",
        "description": "Turtle-style: buy a close above the prior 20-bar high, "
                       "exit below the prior 10-bar low.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "donchian", "params": {"n": 20}, "series": "dc_upper"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "donchian", "params": {"n": 10}, "series": "dc_lower"}}]},
        "tune": [{"path": "entry.0.right.params.n", "label": "entry lookback", "from": 10, "to": 55, "step": 5},
                 {"path": "exit.0.right.params.n",  "label": "exit lookback", "from": 5, "to": 25, "step": 5}],
    },
    {
        "id": "supertrend-follow", "name": "Supertrend Follow", "category": "trend",
        "icon": "⇈", "difficulty": "intermediate",
        "description": "Long whenever the Supertrend flips bullish, flat when "
                       "it flips bearish. ATR-adaptive trailing behaviour built in.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "supertrend", "series": "st_dir"},
                            "op": "crosses_above", "right": {"value": 0}}],
                 "exit":  [{"left": {"kind": "supertrend", "series": "st_dir"},
                            "op": "crosses_below", "right": {"value": 0}}]},
        "tune": [{"path": "entry.0.left.params.n", "label": "ATR period", "from": 7, "to": 21, "step": 7},
                 {"path": "entry.0.left.params.mult", "label": "ATR mult", "values": [2.0, 2.5, 3.0, 3.5, 4.0]}],
    },
    {
        "id": "stoch-dip-buyer", "name": "Stochastic Dip Buyer", "category": "mean-reversion",
        "icon": "⌄", "difficulty": "intermediate",
        "description": "Buy the %K/%D cross while both are washed out below 25; "
                       "take profit when %K reaches 75.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 10,
                 "entry": [{"left": {"kind": "stoch", "series": "stoch_k"},
                            "op": "crosses_above",
                            "right": {"kind": "stoch", "series": "stoch_d"}},
                           {"left": {"kind": "stoch", "series": "stoch_k"},
                            "op": "<", "right": {"value": 25}}],
                 "exit":  [{"left": {"kind": "stoch", "series": "stoch_k"},
                            "op": ">", "right": {"value": 75}}]},
        "tune": [{"path": "entry.1.right.value", "label": "oversold", "from": 15, "to": 35, "step": 5},
                 {"path": "exit.0.right.value",  "label": "target", "from": 65, "to": 90, "step": 5}],
    },
    {
        "id": "keltner-squeeze-break", "name": "Keltner Breakout", "category": "breakout",
        "icon": "⊔", "difficulty": "intermediate",
        "description": "Buy strength through the Keltner upper channel, exit "
                       "when price falls back through the channel midline.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "keltner", "series": "kc_upper"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "keltner", "series": "kc_mid"}}]},
        "tune": [{"path": "entry.0.right.params.n", "label": "EMA period", "from": 14, "to": 30, "step": 4},
                 {"path": "entry.0.right.params.mult", "label": "ATR mult", "values": [1.5, 2.0, 2.5, 3.0]}],
    },
    {
        "id": "psar-flip", "name": "Parabolic SAR Flip", "category": "trend",
        "icon": "◔", "difficulty": "intermediate",
        "description": "Long while price is above the parabolic SAR dots, out "
                       "when it flips underneath — a mechanical trailing system.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "psar"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "psar"}}]},
        "tune": [{"path": "entry.0.right.params.af", "label": "accel", "values": [0.01, 0.02, 0.03, 0.04]},
                 {"path": "entry.0.right.params.max_af", "label": "max accel", "values": [0.1, 0.2, 0.3]}],
    },
    {
        "id": "ichimoku-kumo-break", "name": "Ichimoku Cloud Break", "category": "trend",
        "icon": "☁", "difficulty": "advanced",
        "description": "Buy a close breaking above the cloud (Span A) while "
                       "above Span B too; exit when price loses the Kijun base line.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "ichimoku", "series": "senkou_a"}},
                           {"left": "close", "op": ">",
                            "right": {"kind": "ichimoku", "series": "senkou_b"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "ichimoku", "series": "kijun"}}]},
        "tune": [{"path": "entry.0.right.params.base", "label": "Kijun period", "from": 20, "to": 34, "step": 7}],
    },
    {
        "id": "roc-momo", "name": "Rate-of-Change Momentum", "category": "momentum",
        "icon": "↗", "difficulty": "starter",
        "description": "Buy when 20-bar momentum turns positive above the "
                       "200-bar SMA regime filter, exit when momentum rolls back over.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "roc", "params": {"n": 20}},
                            "op": "crosses_above", "right": {"value": 0}},
                           {"left": "close", "op": ">",
                            "right": {"kind": "sma", "params": {"n": 200}}}],
                 "exit":  [{"left": {"kind": "roc", "params": {"n": 20}},
                            "op": "crosses_below", "right": {"value": 0}}]},
        "tune": [{"path": "entry.0.left.params.n", "label": "ROC period", "from": 10, "to": 40, "step": 5}],
    },
    {
        "id": "vwap-reclaim", "name": "VWAP Reclaim", "category": "momentum",
        "icon": "⚖", "difficulty": "intermediate",
        "description": "Buy price reclaiming the rolling 50-bar VWAP, exit when "
                       "it loses VWAP again — an institutional-level pivot.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "vwap", "params": {"n": 50}}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "vwap", "params": {"n": 50}}}]},
        "tune": [{"path": "entry.0.right.params.n", "label": "VWAP window", "from": 20, "to": 100, "step": 20}],
    },
    {
        "id": "zscore-reversion", "name": "Z-Score Reversion", "category": "mean-reversion",
        "icon": "σ", "difficulty": "advanced",
        "description": "Buy a 2-sigma stretch below the 20-bar mean, exit at "
                       "the mean. Statistical mean reversion, pure and simple.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 12,
                 "entry": [{"left": {"kind": "zscore", "params": {"n": 20}},
                            "op": "<", "right": {"value": -2}}],
                 "exit":  [{"left": {"kind": "zscore", "params": {"n": 20}},
                            "op": ">", "right": {"value": 0}}]},
        "tune": [{"path": "entry.0.right.value", "label": "entry z", "values": [-2.5, -2.0, -1.5]},
                 {"path": "exit.0.right.value",  "label": "exit z", "values": [-0.5, 0.0, 0.5]}],
    },
    {
        "id": "supertrend-long-short", "name": "Supertrend Long/Short", "category": "trend",
        "icon": "⇅", "difficulty": "advanced",
        "description": "Always in the market: long while Supertrend is bullish, "
                       "SHORT while it's bearish. The two-sided flavour of "
                       "Supertrend Follow (native engine only).",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "supertrend", "series": "st_dir"},
                            "op": "crosses_above", "right": {"value": 0}}],
                 "exit":  [{"left": {"kind": "supertrend", "series": "st_dir"},
                            "op": "crosses_below", "right": {"value": 0}}],
                 "short_entry": [{"left": {"kind": "supertrend", "series": "st_dir"},
                                  "op": "crosses_below", "right": {"value": 0}}],
                 "short_exit": [{"left": {"kind": "supertrend", "series": "st_dir"},
                                 "op": "crosses_above", "right": {"value": 0}}]},
        "tune": [{"path": "entry.0.left.params.n", "label": "ATR period", "from": 7, "to": 21, "step": 7},
                 {"path": "entry.0.left.params.mult", "label": "ATR mult", "values": [2.0, 2.5, 3.0, 3.5]}],
    },
    {
        "id": "rsi-fade-short", "name": "RSI Overbought Fade", "category": "mean-reversion",
        "icon": "↧", "difficulty": "advanced",
        "description": "Short-only: fade euphoric RSI > 75 spikes, cover when "
                       "RSI cools below 50. A tight stop caps squeeze risk.",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 8,
                 "short_entry": [{"left": {"kind": "rsi", "params": {"n": 14}},
                                  "op": "crosses_above", "right": {"value": 75}}],
                 "short_exit":  [{"left": {"kind": "rsi", "params": {"n": 14}},
                                  "op": "<", "right": {"value": 50}}]},
        "tune": [{"path": "short_entry.0.right.value", "label": "fade above", "from": 70, "to": 85, "step": 5},
                 {"path": "short_exit.0.right.value", "label": "cover below", "from": 40, "to": 60, "step": 5}],
    },
    {
        "id": "pivot-breakout", "name": "Pivot Breakout", "category": "breakout",
        "icon": "◈", "difficulty": "intermediate",
        "description": "Trade CONFIRMED pivot structure both ways: long on a close "
                       "through the last pivot high, short on a close through the "
                       "last pivot low. Causal zigzag levels — tune the reversal % "
                       "to the asset (this is where it gets reliable).",
        "spec": {"kind": "rule", **_COSTS, "stop_loss_pct": 8,
                 "entry": [{"left": "close", "op": "crosses_above",
                            "right": {"kind": "pivotlevels", "params": {"pct": 5.0},
                                      "series": "pv_high"}}],
                 "exit":  [{"left": "close", "op": "crosses_below",
                            "right": {"kind": "pivotlevels", "params": {"pct": 5.0},
                                      "series": "pv_low"}}],
                 "short_entry": [{"left": "close", "op": "crosses_below",
                                  "right": {"kind": "pivotlevels", "params": {"pct": 5.0},
                                            "series": "pv_low"}}],
                 "short_exit": [{"left": "close", "op": "crosses_above",
                                 "right": {"kind": "pivotlevels", "params": {"pct": 5.0},
                                           "series": "pv_high"}}]},
        "tune": [{"path": "entry.0.right.params.pct", "label": "pivot %",
                  "values": [2.0, 3.0, 5.0, 8.0, 12.0]},
                 {"path": "stop_loss_pct", "label": "stop %", "from": 4, "to": 14, "step": 2}],
    },
    {
        "id": "pivot-trend", "name": "Pivot Trend Rider", "category": "trend",
        "icon": "◇", "difficulty": "starter",
        "description": "Long while the confirmed zigzag leg points up, short while "
                       "it points down — the simplest 'tuned pivots' system. Sweep "
                       "the reversal % to find each asset's rhythm.",
        "spec": {"kind": "rule", **_COSTS,
                 "entry": [{"left": {"kind": "pivotdir", "params": {"pct": 5.0}},
                            "op": "crosses_above", "right": {"value": 0}}],
                 "exit":  [{"left": {"kind": "pivotdir", "params": {"pct": 5.0}},
                            "op": "crosses_below", "right": {"value": 0}}],
                 "short_entry": [{"left": {"kind": "pivotdir", "params": {"pct": 5.0}},
                                  "op": "crosses_below", "right": {"value": 0}}],
                 "short_exit": [{"left": {"kind": "pivotdir", "params": {"pct": 5.0}},
                                 "op": "crosses_above", "right": {"value": 0}}]},
        "tune": [{"path": "entry.0.left.params.pct", "label": "pivot %",
                  "values": [2.0, 3.0, 5.0, 8.0, 12.0, 18.0]}],
    },
    {
        "id": "regime-switcher", "name": "Regime Switcher", "category": "trend",
        "icon": "🌗", "difficulty": "advanced",
        "description": "Multi-stage: rides EMA trends during BULL phases and fades "
                       "overbought RSI (short) during BEAR phases — the phase "
                       "classifier (SMA-200 side + slope) picks which engine runs.",
        "spec": {"kind": "regime", **_COSTS,
                 "regime_source": {"method": "sma", "n": 200},
                 "exit_on_regime_change": True,
                 "regimes": [
                     {"when": "bull",
                      "spec": {"kind": "rule",
                               "entry": [{"left": {"kind": "ema", "params": {"n": 20}},
                                          "op": "crosses_above",
                                          "right": {"kind": "ema", "params": {"n": 50}}}],
                               "exit":  [{"left": {"kind": "ema", "params": {"n": 20}},
                                          "op": "crosses_below",
                                          "right": {"kind": "ema", "params": {"n": 50}}}]}},
                     {"when": "bear",
                      "spec": {"kind": "rule",
                               "short_entry": [{"left": {"kind": "rsi", "params": {"n": 14}},
                                                "op": "crosses_above", "right": {"value": 65}}],
                               "short_exit":  [{"left": {"kind": "rsi", "params": {"n": 14}},
                                                "op": "<", "right": {"value": 45}}]}},
                 ]},
        "tune": [{"path": "regime_source.n", "label": "regime SMA", "from": 100, "to": 250, "step": 50}],
    },
    {
        "id": "ml-classifier-gate", "name": "ML Classifier Gate", "category": "ml",
        "icon": "🧠", "difficulty": "advanced", "needs_model": True,
        "description": "Trade a trained ML predictor directly: long while P(up) "
                       "stays above the entry threshold. Pick one of your trained "
                       "models when instantiating.",
        "spec": {"kind": "ml", **_COSTS, "ml_id": "",
                 "enter_above": 0.6, "exit_below": 0.45},
        "tune": [{"path": "enter_above", "label": "enter above", "values": [0.55, 0.6, 0.65, 0.7]},
                 {"path": "exit_below",  "label": "exit below", "values": [0.35, 0.4, 0.45, 0.5]}],
    },
]

LIBRARY_CATEGORIES = ["trend", "momentum", "mean-reversion", "breakout", "ml"]


# ─────────────────────────────────────────────────────────────────────────────
# Baseline estate + sector map (pure data)
# ─────────────────────────────────────────────────────────────────────────────

BASELINE_ASSETS: List[dict] = [
    # Broad indices
    {"provider": "yahoo", "symbol": "SPY",      "name": "S&P 500",        "group": "Index"},
    {"provider": "yahoo", "symbol": "QQQ",      "name": "Nasdaq 100",     "group": "Index"},
    {"provider": "yahoo", "symbol": "DIA",      "name": "Dow Jones",      "group": "Index"},
    {"provider": "yahoo", "symbol": "IWM",      "name": "Russell 2000",   "group": "Index"},
    # SPDR sectors
    {"provider": "yahoo", "symbol": "XLK",  "name": "Technology",         "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLF",  "name": "Financials",         "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLE",  "name": "Energy",             "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLV",  "name": "Health Care",        "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLY",  "name": "Consumer Disc.",     "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLP",  "name": "Consumer Staples",   "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLI",  "name": "Industrials",        "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLB",  "name": "Materials",          "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLRE", "name": "Real Estate",        "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLU",  "name": "Utilities",          "group": "Sectors"},
    {"provider": "yahoo", "symbol": "XLC",  "name": "Communications",     "group": "Sectors"},
    # Rates / FX / vol
    {"provider": "yahoo", "symbol": "^TNX",     "name": "US 10Y Yield",   "group": "Rates & FX"},
    {"provider": "yahoo", "symbol": "^VIX",     "name": "VIX",            "group": "Rates & FX"},
    {"provider": "yahoo", "symbol": "DX-Y.NYB", "name": "US Dollar",      "group": "Rates & FX"},
    {"provider": "yahoo", "symbol": "TLT",      "name": "20Y+ Treasuries","group": "Rates & FX"},
    # Commodities
    {"provider": "yahoo", "symbol": "GC=F", "name": "Gold",               "group": "Commodities"},
    {"provider": "yahoo", "symbol": "CL=F", "name": "Crude Oil",          "group": "Commodities"},
    # Crypto majors (via yahoo so no exchange dependency)
    {"provider": "yahoo", "symbol": "BTC-USD", "name": "Bitcoin",         "group": "Crypto"},
    {"provider": "yahoo", "symbol": "ETH-USD", "name": "Ethereum",        "group": "Crypto"},
]

_BASELINE_BY_KEY = {f"{a['provider']}:{a['symbol']}": a for a in BASELINE_ASSETS}
GROUP_ORDER = ["Index", "Sectors", "Rates & FX", "Commodities", "Crypto", "Watchlist"]


# ─────────────────────────────────────────────────────────────────────────────
# Macro / on-chain series catalog (pure data)
# ─────────────────────────────────────────────────────────────────────────────
#
# Each entry becomes an ordinary dataset `mkt.macro.<slug>.1d` (close = value)
# so charts can layer it on a secondary scale and strategies can reference it.

MACRO_CATALOG: List[dict] = [
    {"id": "fred:FEDFUNDS",  "name": "Fed Funds Rate",       "unit": "%",    "group": "Rates",     "source": "fred"},
    {"id": "fred:DGS10",     "name": "US 10Y Treasury",      "unit": "%",    "group": "Rates",     "source": "fred"},
    {"id": "fred:DGS2",      "name": "US 2Y Treasury",       "unit": "%",    "group": "Rates",     "source": "fred"},
    {"id": "fred:T10Y2Y",    "name": "10Y–2Y Spread",        "unit": "%",    "group": "Rates",     "source": "fred"},
    {"id": "fred:CPIAUCSL",  "name": "CPI (index)",          "unit": "idx",  "group": "Inflation", "source": "fred"},
    {"id": "fred:CPI_YOY",   "name": "Inflation YoY",        "unit": "%",    "group": "Inflation", "source": "fred",
     "derived_from": "CPIAUCSL", "derive": "yoy"},
    {"id": "fred:UNRATE",    "name": "Unemployment",         "unit": "%",    "group": "Economy",   "source": "fred"},
    {"id": "fred:M2SL",      "name": "M2 Money Supply",      "unit": "$B",   "group": "Economy",   "source": "fred"},
    {"id": "fred:WALCL",     "name": "Fed Balance Sheet",    "unit": "$M",   "group": "Economy",   "source": "fred"},
    {"id": "chain:hash-rate",           "name": "BTC Hash Rate",      "unit": "TH/s", "group": "On-chain", "source": "chain"},
    {"id": "chain:n-transactions",      "name": "BTC Transactions",   "unit": "tx/d", "group": "On-chain", "source": "chain"},
    {"id": "chain:n-unique-addresses",  "name": "BTC Active Addresses","unit": "addr", "group": "On-chain", "source": "chain"},
    {"id": "chain:total-bitcoins",      "name": "BTC Supply",         "unit": "BTC",  "group": "Token economics", "source": "chain"},
    {"id": "chain:miners-revenue",      "name": "BTC Miner Revenue",  "unit": "$",    "group": "Token economics", "source": "chain"},
    {"id": "chain:market-cap",          "name": "BTC Market Cap",     "unit": "$",    "group": "Token economics", "source": "chain"},
]

_MACRO_BY_ID = {m["id"]: m for m in MACRO_CATALOG}


# ─────────────────────────────────────────────────────────────────────────────
# Key-date library (pure data)
# ─────────────────────────────────────────────────────────────────────────────

def _d(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())

EVENT_LIBRARY: List[dict] = [
    # Bitcoin halvings (block-schedule; last one projected)
    {"kind": "halving", "t": _d("2012-11-28"), "text": "BTC halving #1 (50→25)",   "match": ["btc", "bitcoin"]},
    {"kind": "halving", "t": _d("2016-07-09"), "text": "BTC halving #2 (25→12.5)", "match": ["btc", "bitcoin"]},
    {"kind": "halving", "t": _d("2020-05-11"), "text": "BTC halving #3 (12.5→6.25)","match": ["btc", "bitcoin"]},
    {"kind": "halving", "t": _d("2024-04-20"), "text": "BTC halving #4 (6.25→3.125)","match": ["btc", "bitcoin"]},
    {"kind": "halving", "t": _d("2028-04-14"), "text": "BTC halving #5 (projected)","match": ["btc", "bitcoin"], "projected": True},
    # Litecoin halvings
    {"kind": "halving", "t": _d("2015-08-25"), "text": "LTC halving #1", "match": ["ltc", "litecoin"]},
    {"kind": "halving", "t": _d("2019-08-05"), "text": "LTC halving #2", "match": ["ltc", "litecoin"]},
    {"kind": "halving", "t": _d("2023-08-02"), "text": "LTC halving #3", "match": ["ltc", "litecoin"]},
    {"kind": "halving", "t": _d("2027-07-30"), "text": "LTC halving #4 (projected)", "match": ["ltc", "litecoin"], "projected": True},
    # Ethereum upgrades
    {"kind": "upgrade", "t": _d("2021-08-05"), "text": "ETH London (EIP-1559)", "match": ["eth", "ethereum"]},
    {"kind": "upgrade", "t": _d("2022-09-15"), "text": "ETH Merge (PoS)",       "match": ["eth", "ethereum"]},
    {"kind": "upgrade", "t": _d("2024-03-13"), "text": "ETH Dencun (blobs)",    "match": ["eth", "ethereum"]},
    # Market-wide shocks & milestones (match everything)
    {"kind": "macro", "t": _d("2008-09-15"), "text": "Lehman collapse",        "match": ["*"]},
    {"kind": "macro", "t": _d("2020-03-12"), "text": "COVID crash",            "match": ["*"]},
    {"kind": "macro", "t": _d("2022-11-08"), "text": "FTX collapse",           "match": ["*"]},
    {"kind": "macro", "t": _d("2024-01-10"), "text": "Spot BTC ETFs approved", "match": ["*"]},
    {"kind": "macro", "t": _d("2016-11-08"), "text": "US election 2016",       "match": ["*"]},
    {"kind": "macro", "t": _d("2020-11-03"), "text": "US election 2020",       "match": ["*"]},
    {"kind": "macro", "t": _d("2024-11-05"), "text": "US election 2024",       "match": ["*"]},
]

EVENT_COLORS = {"halving": "#e8b34d", "upgrade": "#b48ead", "macro": "#8a93a6",
                "ipo": "#5ec9b8", "dividend": "#7aa2f7", "split": "#d1848b"}


# ─────────────────────────────────────────────────────────────────────────────
# Pure math helpers (importable standalone for tests)
# ─────────────────────────────────────────────────────────────────────────────

def rdp_indices(x: "np.ndarray", y: "np.ndarray", eps: float,
                max_points: int = 80) -> List[int]:
    """Ramer–Douglas–Peucker on a polyline given as parallel x/y arrays
    (both should be pre-normalised to comparable scales). Returns the kept
    indices (always includes first + last), i.e. the pivot points."""
    n = len(x)
    if n < 3:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 < 2:
            continue
        dx, dy = x[i1] - x[i0], y[i1] - y[i0]
        seg_len = math.hypot(dx, dy)
        xs, ys = x[i0 + 1:i1], y[i0 + 1:i1]
        if seg_len < 1e-12:
            d = np.hypot(xs - x[i0], ys - y[i0])
        else:
            d = np.abs(dy * xs - dx * ys + x[i1] * y[i0] - y[i1] * x[i0]) / seg_len
        j = int(np.argmax(d))
        if d[j] > eps and len(keep) < max_points:
            idx = i0 + 1 + j
            keep.add(idx)
            stack.append((i0, idx))
            stack.append((idx, i1))
    return sorted(keep)


def ols_fit(x: "np.ndarray", y: "np.ndarray"):
    """Least-squares line fit → (slope, intercept, r2)."""
    n = len(x)
    if n < 2:
        return 0.0, float(y[0]) if n else 0.0, 0.0
    xm, ym = float(np.mean(x)), float(np.mean(y))
    dx = x - xm
    denom = float(np.sum(dx * dx))
    if denom < 1e-18:
        return 0.0, ym, 0.0
    b = float(np.sum(dx * (y - ym))) / denom
    a = ym - b * xm
    resid = y - (a + b * x)
    ss_res = float(np.sum(resid * resid))
    ss_tot = float(np.sum((y - ym) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 1.0
    return b, a, r2


def detail_to_eps(detail: float) -> float:
    """Map the UI's 0–100 detail knob to an RDP epsilon on unit-normalised
    data. 0 → very coarse (1-3 segments), 100 → fine (many segments)."""
    d = max(0.0, min(100.0, float(detail)))
    return 0.28 * math.exp(-d / 18.0) + 0.004


def trendfit(arr: Dict[str, "np.ndarray"], detail: float = 50.0,
             log_scale: bool = True, flat_pct_year: float = 12.0) -> dict:
    """Overall OLS fit + regression channel + RDP piecewise segments with
    bull/bear/flat labels. All outputs are in price space."""
    t = arr["t"].astype(np.float64)
    c = np.maximum(1e-12, arr["c"].astype(np.float64))
    n = len(c)
    if n < 10:
        return {"error": "need at least 10 bars"}
    y = np.log(c) if log_scale else c.copy()
    years_total = max(1e-9, (t[-1] - t[0]) / (365.0 * 86400.0))

    def back(v):
        return float(np.exp(v)) if log_scale else float(v)

    def annual_pct(slope_per_x, x_span, y0v, seg_years):
        """Slope over normalised x → annualised % change."""
        dy = slope_per_x * x_span
        if seg_years <= 1e-9:
            return 0.0
        if log_scale:
            return (math.exp(dy / seg_years) - 1.0) * 100.0
        base = back(y0v)
        return (dy / seg_years) / max(1e-9, base) * 100.0

    # normalise for fitting
    xn = (t - t[0]) / max(1.0, (t[-1] - t[0]))
    y_lo, y_hi = float(np.min(y)), float(np.max(y))
    yn = (y - y_lo) / max(1e-12, (y_hi - y_lo))

    # overall fit + channel
    b, a, r2 = ols_fit(xn, y)
    resid = y - (a + b * xn)
    sd = float(np.std(resid))
    overall = {
        "t0": int(t[0]), "t1": int(t[-1]),
        "p0": back(a), "p1": back(a + b),
        "upper0": back(a + 2 * sd), "upper1": back(a + b + 2 * sd),
        "lower0": back(a - 2 * sd), "lower1": back(a + b - 2 * sd),
        "r2": round(r2, 4),
        "slope_pct_year": round(annual_pct(b, 1.0, a, years_total), 3),
    }

    # piecewise segments at pivots
    eps = detail_to_eps(detail)
    idx = rdp_indices(xn, yn, eps)
    segments = []
    for k in range(len(idx) - 1):
        i0, i1 = idx[k], idx[k + 1]
        if i1 - i0 < 1:
            continue
        sb, sa, sr2 = ols_fit(xn[i0:i1 + 1], y[i0:i1 + 1])
        x0v, x1v = xn[i0], xn[i1]
        y0f, y1f = sa + sb * x0v, sa + sb * x1v
        seg_years = max(1e-9, (t[i1] - t[i0]) / (365.0 * 86400.0))
        apct = annual_pct(sb, x1v - x0v, y0f, seg_years)
        label = "bull" if apct >= flat_pct_year else ("bear" if apct <= -flat_pct_year else "flat")
        segments.append({
            "t0": int(t[i0]), "t1": int(t[i1]),
            "p0": back(y0f), "p1": back(y1f),
            "ret_pct": round((back(y1f) / max(1e-12, back(y0f)) - 1.0) * 100.0, 3),
            "slope_pct_year": round(apct, 3),
            "r2": round(sr2, 4), "label": label, "bars": int(i1 - i0),
        })
    return {
        "bars": n, "detail": float(detail), "eps": round(eps, 5),
        "log_scale": bool(log_scale),
        "overall": overall,
        "segments": segments,
        "pivots": [int(t[i]) for i in idx],
        "regime_now": segments[-1]["label"] if segments else "flat",
    }


def equity_analytics(eq_t: List[int], eq: List[float],
                     trades: List[dict], bpy: float = 365.0) -> dict:
    """Rich analytics over an equity curve + trade list (any resolution)."""
    t = np.asarray(eq_t, dtype=np.float64)
    e = np.asarray(eq, dtype=np.float64)
    n = len(e)
    out: dict = {}
    if n < 3:
        return out
    peak = np.maximum.accumulate(e)
    dd = (e - peak) / np.maximum(1e-12, peak) * 100.0

    def _ds(vals, cap=800):
        if len(vals) <= cap:
            return list(range(len(vals)))
        step = len(vals) / cap
        return sorted({int(i * step) for i in range(cap)} | {len(vals) - 1})

    di = _ds(dd)
    out["drawdown"] = {"t": [int(t[i]) for i in di],
                      "dd_pct": [round(float(dd[i]), 3) for i in di]}
    out["max_drawdown_pct"] = round(float(np.min(dd)), 3)

    # longest underwater stretch
    uw_start, uw_best, cur = None, 0.0, None
    for i in range(n):
        if dd[i] < -0.01:
            if cur is None:
                cur = t[i]
        else:
            if cur is not None:
                uw_best = max(uw_best, t[i] - cur)
                cur = None
    if cur is not None:
        uw_best = max(uw_best, t[-1] - cur)
    out["longest_underwater_days"] = round(uw_best / 86400.0, 1)

    # monthly returns
    months: Dict[str, float] = {}
    for i in range(n):
        ym = datetime.utcfromtimestamp(int(t[i])).strftime("%Y-%m")
        months[ym] = float(e[i])                 # last equity seen per month
    keys = sorted(months.keys())
    monthly = []
    prev = None
    for k in keys:
        v = months[k]
        if prev is not None and prev > 1e-12:
            monthly.append({"ym": k, "ret_pct": round((v / prev - 1.0) * 100.0, 3)})
        prev = v
    out["monthly"] = monthly[-120:]

    # rolling sharpe + rolling return
    rets = np.diff(e) / np.maximum(1e-12, e[:-1])
    w = int(max(20, min(252, n // 8)))
    if len(rets) > w + 2:
        rs_t, rs_v, rr_v = [], [], []
        for i in range(w, len(rets)):
            wnd = rets[i - w:i]
            sdv = float(np.std(wnd))
            rs = float(np.mean(wnd)) / sdv * math.sqrt(bpy) if sdv > 1e-12 else 0.0
            rs_t.append(int(t[i + 1])); rs_v.append(rs)
            rr_v.append(float(e[i + 1] / e[i + 1 - w] - 1.0) * 100.0)
        ri = _ds(rs_v, 400)
        out["rolling"] = {"window_bars": w,
                          "t": [rs_t[i] for i in ri],
                          "sharpe": [round(rs_v[i], 3) for i in ri],
                          "ret_pct": [round(rr_v[i], 3) for i in ri]}

    # trade distribution + streaks + reasons
    rp = [float(tr.get("ret_pct") or 0.0) for tr in (trades or [])]
    if rp:
        lo, hi = min(rp), max(rp)
        span = max(1e-9, hi - lo)
        nb = min(21, max(7, int(math.sqrt(len(rp)) * 2)))
        edges = [lo + span * i / nb for i in range(nb + 1)]
        counts = [0] * nb
        for v in rp:
            j = min(nb - 1, int((v - lo) / span * nb))
            counts[j] += 1
        out["histogram"] = {"edges": [round(x, 3) for x in edges], "counts": counts}
        best_w = best_l = cw = cl = 0
        for v in rp:
            if v > 0:
                cw += 1; cl = 0
            else:
                cl += 1; cw = 0
            best_w = max(best_w, cw); best_l = max(best_l, cl)
        out["streaks"] = {"max_wins": best_w, "max_losses": best_l}
        reasons: Dict[str, int] = {}
        for tr in trades:
            r = str(tr.get("reason") or "signal")
            reasons[r] = reasons.get(r, 0) + 1
        out["exit_reasons"] = reasons
        durs = [tr.get("bars") or 0 for tr in trades]
        out["avg_trade_bars"] = round(float(np.mean(durs)), 1) if durs else None
    return out


def _zigzag_core(t: "np.ndarray", c: "np.ndarray", rev_amount) -> List[dict]:
    """Shared ZigZag state machine. `rev_amount(i, ext_px)` returns the
    absolute price move required at bar i to confirm a reversal.

    The direction must be SEEDED before extremes are tracked one-sided —
    tracking both extremes with a shared cursor while direction is unknown
    lets a falling price overwrite the recorded high, so no reversal can
    ever confirm (the original single-cursor version returned one pivot)."""
    n = len(c)
    if n < 3:
        return []
    piv: List[dict] = []
    hi_i = lo_i = 0
    direction, ext_i = 0, 0
    for i in range(1, n):
        if direction == 0:                      # seed from the first confirmed swing
            if c[i] > c[hi_i]:
                hi_i = i
            if c[i] < c[lo_i]:
                lo_i = i
            if (c[i] - c[lo_i]) >= rev_amount(i, c[lo_i]):
                piv.append({"t": int(t[lo_i]), "p": float(c[lo_i]), "kind": "low"})
                direction, ext_i = 1, i
            elif (c[hi_i] - c[i]) >= rev_amount(i, c[hi_i]):
                piv.append({"t": int(t[hi_i]), "p": float(c[hi_i]), "kind": "high"})
                direction, ext_i = -1, i
        elif direction == 1:                     # rising leg — track the high
            if c[i] > c[ext_i]:
                ext_i = i
            elif (c[ext_i] - c[i]) >= rev_amount(i, c[ext_i]):
                piv.append({"t": int(t[ext_i]), "p": float(c[ext_i]), "kind": "high"})
                direction, ext_i = -1, i
        else:                                    # falling leg — track the low
            if c[i] < c[ext_i]:
                ext_i = i
            elif (c[i] - c[ext_i]) >= rev_amount(i, c[ext_i]):
                piv.append({"t": int(t[ext_i]), "p": float(c[ext_i]), "kind": "low"})
                direction, ext_i = 1, i
    if direction != 0:
        piv.append({"t": int(t[ext_i]), "p": float(c[ext_i]),
                    "kind": "high" if direction == 1 else "low"})
    return piv


def zigzag_pivots(t: "np.ndarray", c: "np.ndarray", pct: float = 5.0) -> List[dict]:
    """Classic ZigZag: a pivot confirms when price retraces `pct`% from the
    running extreme. Returns [{t,p,kind:'high'|'low'}]."""
    thr = max(0.05, float(pct)) / 100.0
    return _zigzag_core(t, c, lambda i, px: abs(px) * thr)


def atr_zigzag_pivots(h, l, c, t, atr: "np.ndarray", mult: float = 3.0) -> List[dict]:
    """ZigZag with an ATR-scaled reversal threshold — adapts to volatility.
    NaN ATR (warmup) means no reversal can confirm there."""
    m = max(0.1, float(mult))

    def rev(i, _px):
        a = atr[i]
        return float("inf") if math.isnan(a) else max(1e-9, m * a)
    return _zigzag_core(t, c, rev)


def fractal_pivots(h, l, t, n_side: int = 2) -> List[dict]:
    """Williams fractals: a bar whose high (low) is the extreme of `n_side`
    bars on each side."""
    n = len(h)
    k = max(1, int(n_side))
    piv: List[dict] = []
    for i in range(k, n - k):
        w_h = h[i - k: i + k + 1]
        w_l = l[i - k: i + k + 1]
        if h[i] >= np.max(w_h):
            piv.append({"t": int(t[i]), "p": float(h[i]), "kind": "high"})
        elif l[i] <= np.min(w_l):
            piv.append({"t": int(t[i]), "p": float(l[i]), "kind": "low"})
    return piv


PIVOT_METHODS = ["zigzag", "atr_zigzag", "fractal", "rdp"]


def position_series(n: int, t: "np.ndarray", trades: List[dict]) -> List[int]:
    """Per-bar in-position flags (0/1) reconstructed from the trade list."""
    pos = np.zeros(n, dtype=np.int8)
    ts = t.astype(np.int64)
    for tr in trades or []:
        try:
            i0 = int(np.searchsorted(ts, int(tr["entry_t"]), side="left"))
            i1 = int(np.searchsorted(ts, int(tr.get("exit_t") or ts[-1]), side="right"))
            pos[i0:i1] = 1
        except Exception:
            continue
    return pos.tolist()


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE and HAS_NUMPY:

    def _md():
        return sys.modules.get("markets_data_capabilities")

    def _ma():
        return sys.modules.get("markets_analysis_capabilities")

    def _mlab():
        return sys.modules.get("markets_lab_capabilities")

    def _mc():
        return sys.modules.get("markets_capabilities")

    _TABLES_READY = False

    def _ensure_tables_sync():
        conn = _sqlite_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_at TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_sim_accounts (
                    id         TEXT PRIMARY KEY,
                    name       TEXT,
                    start_cash REAL,
                    cash       REAL,
                    currency   TEXT DEFAULT 'USD',
                    meta       TEXT,
                    created_at TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_sim_orders (
                    id         TEXT PRIMARY KEY,
                    account_id TEXT,
                    symbol_key TEXT,
                    side       TEXT,
                    qty        REAL,
                    price      REAL,
                    fees       REAL DEFAULT 0,
                    note       TEXT,
                    source     TEXT,
                    ts         TEXT
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS mkt_simord_acct "
                         "ON mkt_sim_orders(account_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_sim_equity (
                    id         TEXT PRIMARY KEY,
                    account_id TEXT,
                    ts         TEXT,
                    value      REAL,
                    cash       REAL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS mkt_simeq_acct "
                         "ON mkt_sim_equity(account_id)")
            conn.commit()
        finally:
            conn.close()

    async def _ensure_tables():
        global _TABLES_READY
        if _TABLES_READY:
            return
        await asyncio.get_running_loop().run_in_executor(None, _ensure_tables_sync)
        _TABLES_READY = True

    def _kv_get_sync(key: str):
        conn = _sqlite_conn()
        try:
            r = conn.execute("SELECT value FROM mkt_settings WHERE key=?", (key,)).fetchone()
            if not r or not r[0]:
                return None
            try:
                return json.loads(r[0])
            except Exception:
                return None
        finally:
            conn.close()

    def _kv_set_sync(key: str, value):
        conn = _sqlite_conn()
        try:
            conn.execute("INSERT OR REPLACE INTO mkt_settings (key,value,updated_at) "
                         "VALUES (?,?,?)", (key, json.dumps(value), now_iso()))
            conn.commit()
        finally:
            conn.close()

    def _kv_del_sync(key: str):
        conn = _sqlite_conn()
        try:
            conn.execute("DELETE FROM mkt_settings WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()

    def _kv_scan_sync(prefix: str) -> List[dict]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT key,value,updated_at FROM mkt_settings WHERE key LIKE ? "
                "ORDER BY updated_at DESC", (prefix + "%",)).fetchall()
            out = []
            for r in rows:
                try:
                    v = json.loads(r[1]) if r[1] else {}
                except Exception:
                    v = {}
                out.append({"key": r[0], "value": v, "updated_at": r[2]})
            return out
        finally:
            conn.close()

    # ── Strategy library ─────────────────────────────────────────────────────

    @capability(
        "markets.strategy.library", http_method="GET",
        http_path="/markets/strategy/library", http_tags=["markets"],
        memory="off", silent=True,
        description="Built-in strategy library: ready-to-run templates (trend / "
                    "momentum / mean-reversion / breakout / ml) with full specs and "
                    "suggested tuning axes for sweeps & autotune. Input: category "
                    "(str — filter). Output: {templates:[{id,name,category,icon,"
                    "description,difficulty,spec,tune,needs_model}], categories}.",
    )
    async def cap_strategy_library(category: str = "", trace_id=None) -> dict:
        items = STRATEGY_LIBRARY
        if category:
            items = [x for x in items if x.get("category") == category]
        return {"templates": items, "count": len(items),
                "categories": LIBRARY_CATEGORIES}

    @capability(
        "markets.strategy.from_template", http_method="POST",
        http_path="/markets/strategy/from_template", http_tags=["markets"], memory="on",
        description="Instantiate a library template as a saved strategy — no JSON "
                    "editing. Input: template_id (str!), name (str — default template "
                    "name), overrides (object {dotted.path: value} — e.g. "
                    "{'entry.0.left.params.n': 30, 'ml_id': 'abc123'}). "
                    "Output: {ok, id, name, kind, spec}.",
    )
    async def cap_strategy_from_template(template_id: str = "", name: str = "",
                                         overrides=None, trace_id=None) -> dict:
        tpl = next((x for x in STRATEGY_LIBRARY if x["id"] == template_id), None)
        if not tpl:
            return {"error": f"unknown template '{template_id}'",
                    "valid": [x["id"] for x in STRATEGY_LIBRARY]}
        lab = _mlab()
        if not lab:
            return {"error": "markets lab module unavailable"}
        spec = json.loads(json.dumps(tpl["spec"]))
        if isinstance(overrides, str):
            try:
                overrides = json.loads(overrides)
            except Exception:
                overrides = None
        if isinstance(overrides, dict):
            for path, val in overrides.items():
                try:
                    lab.set_spec_path(spec, str(path), val)
                except Exception as e:
                    return {"error": f"bad override '{path}': {e}"}
        if tpl.get("needs_model") and not spec.get("ml_id"):
            return {"error": "this template needs an ml_id override — train a model "
                             "first (markets.ml.create) and pass overrides={'ml_id': …}"}
        r = await lab.cap_strategy_save(name=(name or tpl["name"]).strip(),
                                        spec=spec, kind=spec.get("kind") or "rule")
        if r.get("error"):
            return r
        await emit_event({"type": "markets.strategy", "stage": "from_template",
                          "template": template_id, "id": r.get("id")})
        return {"ok": True, "id": r.get("id"), "name": name or tpl["name"],
                "kind": r.get("kind"), "spec": spec, "tune": tpl.get("tune") or []}

    # ── Trend / regime fit ───────────────────────────────────────────────────

    @capability(
        "markets.analysis.trendfit", http_method="POST",
        http_path="/markets/analysis/trendfit", http_tags=["markets"],
        memory="off", silent=True,
        description="Fit trend lines to stored bars: one overall least-squares fit "
                    "with a ±2σ regression channel, PLUS a piecewise multi-segment "
                    "fit whose segments sit at price pivots (RDP) and are labelled "
                    "bull/bear/flat by annualised slope. `detail` 0-100 controls "
                    "segmentation granularity (0=coarse regime view, 100=fine). "
                    "Input: dataset_id (str!), detail (int=50), log_scale (bool=True), "
                    "flat_pct_year (float=12 — |annualised slope| below this = flat), "
                    "limit (int=5000), start/end (ISO str). Output: {overall:{t0,t1,"
                    "p0,p1,upper*,lower*,r2,slope_pct_year}, segments:[{t0,t1,p0,p1,"
                    "ret_pct,slope_pct_year,label,r2}], pivots:[ts], regime_now}.",
    )
    async def cap_trendfit(dataset_id: str = "", detail: int = 50,
                           log_scale: bool = True, flat_pct_year: float = 12.0,
                           limit: int = 5000, start: str = "", end: str = "",
                           trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        md, ma = _md(), _ma()
        if not (md and ma):
            return {"error": "markets data/analysis modules unavailable"}
        bars = await md.get_bars(dataset_id, max(50, min(50_000, int(limit))),
                                 md._iso_to_ms(start), md._iso_to_ms(end))
        if len(bars) < 10:
            return {"error": f"only {len(bars)} bars stored for {dataset_id}"}
        arr = ma.bars_to_arrays(bars)
        res = await asyncio.get_running_loop().run_in_executor(
            None, trendfit, arr, float(detail), bool(log_scale), float(flat_pct_year))
        res["dataset_id"] = dataset_id
        return res

    # ── Backtest deep analytics + replay ─────────────────────────────────────

    @capability(
        "markets.backtest.analyze", http_method="GET",
        http_path="/markets/backtest/analyze", http_tags=["markets"],
        memory="off", silent=True,
        description="Deep analytics for a stored backtest: drawdown curve, longest "
                    "underwater stretch, monthly-return heat-map, rolling Sharpe & "
                    "return, trade histogram, win/loss streaks, exit-reason mix. "
                    "With replay=true the strategy is re-simulated at FULL resolution "
                    "and per-bar equity + position + every trade are returned so the "
                    "UI can play the backtest through the chart. Input: id (str!), "
                    "replay (bool=False), limit (int=20000 bars for replay). "
                    "Output: {id, stats, analytics:{…}, replay?:{t,equity,position,"
                    "trades,entries,exits}}.",
    )
    async def cap_backtest_analyze(id: str = "", replay: bool = False,
                                   limit: int = 20000, trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        lab, md, ma = _mlab(), _md(), _ma()
        if not (lab and md and ma):
            return {"error": "markets modules unavailable"}
        row = await lab.cap_backtest_get(id=id)
        if row.get("error"):
            return row
        out = {"id": id, "name": row.get("name"), "dataset_id": row.get("dataset_id"),
               "strategy_id": row.get("strategy_id"), "stats": row.get("stats") or {}}

        eq_t = row.get("equity_t") or []
        eq = row.get("equity") or []
        trades = row.get("trades") or []
        rep = None
        if replay and row.get("spec") and row.get("dataset_id"):
            try:
                bars = await md.get_bars(row["dataset_id"],
                                         max(200, min(100_000, int(limit))))
                if len(bars) >= 50:
                    arr = ma.bars_to_arrays(bars)
                    entry, exit_, s_en, s_ex = await lab._spec_signals(arr, row["spec"])
                    res = await asyncio.get_running_loop().run_in_executor(
                        None, lab.run_backtest, arr, entry, exit_, row["spec"],
                        s_en, s_ex)
                    eq_t, eq, trades = res["equity_t"], res["equity"], res["trades"]
                    rep = {
                        "t": [int(x) for x in arr["t"].tolist()],
                        "equity": eq,
                        "position": position_series(len(eq), arr["t"], trades),
                        "trades": trades,
                        "entries": [int(arr["t"][i]) for i in np.where(entry)[0]][-2000:],
                        "exits": [int(arr["t"][i]) for i in np.where(exit_)[0]][-2000:],
                        "stats": res["stats"],
                    }
                    out["stats"] = res["stats"]
            except Exception as e:
                out["replay_error"] = str(e)[:300]
        if len(eq) >= 3:
            bpy = 365.0
            try:
                bpy = lab.infer_bars_per_year(np.asarray(eq_t, dtype=np.int64))
            except Exception:
                pass
            out["analytics"] = await asyncio.get_running_loop().run_in_executor(
                None, equity_analytics, eq_t, eq, trades, bpy)
        else:
            out["analytics"] = {}
        if rep:
            out["replay"] = rep
        return out

    # ── Deterministic autotune ───────────────────────────────────────────────

    _AUTOTUNES: Dict[str, dict] = {}
    _TUNE_SKIP_KEYS = {"fee_bps", "slippage_bps", "size_pct", "leverage"}

    def discover_axes(spec: dict) -> List[dict]:
        """Numeric leaves of a strategy spec worth tuning, best-first."""
        found: List[dict] = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    p = f"{path}.{k}" if path else str(k)
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)):
                        if k in _TUNE_SKIP_KEYS:
                            continue
                        found.append({"path": p, "value": float(v),
                                      "int": isinstance(v, int), "key": str(k)})
                    elif isinstance(v, (dict, list)):
                        walk(v, p)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}.{i}")
        walk(spec, "")

        def rank(ax):
            if ".params." in ax["path"]:
                return 0
            if ax["key"] in ("value", "enter_above", "exit_below"):
                return 1
            if ax["key"] in ("stop_loss_pct", "take_profit_pct"):
                return 2
            return 3
        found.sort(key=rank)
        return found

    def _axis_values(center: float, is_int: bool, span: float, steps: int,
                     key: str) -> Optional[List[float]]:
        if abs(center) < 1e-12:
            if key in ("stop_loss_pct", "take_profit_pct"):
                return [0.0, 2.0, 5.0, 8.0][:max(2, steps)]
            return None
        lo, hi = center * (1 - span), center * (1 + span)
        if lo > hi:
            lo, hi = hi, lo
        if steps < 2:
            return [center]
        vals = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
        if is_int:
            out = sorted({max(2, int(round(v))) for v in vals})
        else:
            out = sorted({round(v, 6) for v in vals})
        return [float(v) for v in out]

    async def _autotune_task(aid: str):
        st = _AUTOTUNES[aid]
        lab = _mlab()
        loop = asyncio.get_running_loop()
        try:
            arr_full = st.pop("_arr")
            spec = json.loads(json.dumps(st["spec"]))
            metric = st["metric"]
            axes = st["axes"]
            rounds = st["rounds"]
            per_round = st["per_round"]
            min_trades = int(st.get("min_trades") or 0)
            explore = int(st.get("explore") or 0)
            # hold out the tail as OUT-OF-SAMPLE: the search only ever sees the
            # in-sample slice; the winner is re-scored on the unseen tail so an
            # overfit "improvement" is visible instead of silently shipped
            osf = float(st.get("oos_split") or 0)
            n_all = len(arr_full["t"])
            split_i = int(n_all * (1 - osf)) if 0.05 <= osf <= 0.6 else n_all
            if n_all - split_i < 100:
                split_i = n_all
            arr = {k: v[:split_i] for k, v in arr_full.items()}
            st["oos_bars"] = int(n_all - split_i)
            centers = {ax["path"]: ax["value"] for ax in axes}
            is_int = {ax["path"]: ax["int"] for ax in axes}
            keys = {ax["path"]: ax["key"] for ax in axes}
            rng = np.random.default_rng(17)

            def apply_vals(s, vals):
                s = json.loads(json.dumps(s))
                for p, v in vals.items():
                    vv = int(v) if is_int.get(p) and float(v).is_integer() else float(v)
                    lab.set_spec_path(s, p, vv)
                return s

            def _mval(stats):
                """Metric extraction incl. the 'blend' composite (Sharpe-led,
                drawdown- and profit-factor-aware) — resists single-metric
                overfitting."""
                if not stats:
                    return None
                if metric == "blend":
                    sh = stats.get("sharpe") or 0.0
                    cal = stats.get("calmar")
                    cal = max(-5.0, min(5.0, cal)) if cal is not None else 0.0
                    pf = stats.get("profit_factor")
                    pfl = math.log(max(0.05, min(20.0, pf))) if pf else 0.0
                    v = round(0.5 * sh + 0.3 * cal + 0.2 * pfl, 4)
                    stats["blend"] = v
                    return v
                return stats.get(metric)

            async def evaluate(vals, on=None):
                s = apply_vals(spec, vals)
                a = on if on is not None else arr
                entry, exit_, s_en, s_ex = await lab._spec_signals(a, s)
                res = await loop.run_in_executor(None, lab.run_backtest,
                                                 a, entry, exit_, s, s_en, s_ex)
                stats = res["stats"]
                if on is None and min_trades and (stats.get("trades") or 0) < min_trades:
                    return None                    # not enough trades to trust
                return stats

            # baseline (kept even when it fails the min-trades gate)
            base_stats = await evaluate(centers) or await evaluate(centers, arr) or {}
            _mval(base_stats)
            best = {"values": dict(centers), "stats": base_stats,
                    "metric": _mval(base_stats)}
            finalists = []                       # top in-sample candidates
            st["baseline"] = base_stats
            st["best"] = best
            history: List[dict] = []
            span = 0.5
            paths = [ax["path"] for ax in axes]
            evals_done = 0

            async def try_vals(vals):
                nonlocal evals_done
                stats = None
                try:
                    stats = await evaluate(vals)
                except Exception as e:
                    history.append({"values": vals, "error": str(e)[:160]})
                evals_done += 1
                st["done"] = evals_done
                return stats

            for rnd in range(rounds):
                if st.get("cancel"):
                    st["status"] = "cancelled"
                    break
                # coordinate-descent: rotate through chunks of ≤3 axes
                chunk = paths[(rnd * 3) % len(paths):(rnd * 3) % len(paths) + 3] \
                    if len(paths) > 3 else paths
                if not chunk:
                    chunk = paths[:3]
                steps = max(3, min(7, int(round(per_round ** (1.0 / max(1, len(chunk)))))))
                grids = {}
                for p in chunk:
                    vals = _axis_values(best["values"][p], is_int.get(p, False),
                                        span, steps, keys.get(p, ""))
                    if vals:
                        grids[p] = vals
                if not grids:
                    break
                combos: List[dict] = [{}]
                for p, vals in grids.items():
                    combos = [{**c, p: v} for c in combos for v in vals]
                combos = combos[:per_round]
                # random exploration escapes the local grid (wider jitter)
                for _ in range(explore):
                    jitter = {}
                    for p in chunk:
                        bv = best["values"][p]
                        if abs(bv) < 1e-12:
                            continue
                        vv = bv * (1 + float(rng.uniform(-span * 1.8, span * 1.8)))
                        jitter[p] = max(2, int(round(vv))) if is_int.get(p) \
                            else round(float(vv), 6)
                    if jitter:
                        combos.append(jitter)
                improved = False
                for i, combo in enumerate(combos):
                    if st.get("cancel"):
                        break
                    vals = {**best["values"], **combo}
                    stats = await try_vals(vals)
                    if stats is not None:
                        mv = _mval(stats)
                        if mv is not None:
                            sig2 = json.dumps(vals, sort_keys=True)
                            if not any(f["sig"] == sig2 for f in finalists):
                                finalists.append({"values": vals, "m": mv, "sig": sig2})
                                finalists.sort(key=lambda f: -f["m"])
                                del finalists[8:]
                        if mv is not None and (best["metric"] is None or mv > best["metric"]):
                            best = {"values": vals, "stats": stats, "metric": mv}
                            st["best"] = best
                            improved = True
                    if (i + 1) % max(1, len(combos) // 8) == 0 or i + 1 == len(combos):
                        await emit_event({"type": "markets.backtest",
                                          "stage": "autotune_progress", "autotune_id": aid,
                                          "round": rnd + 1, "rounds": rounds,
                                          "done": evals_done, "total": st["total_est"],
                                          "best_metric": best["metric"],
                                          "metric": metric})
                history.append({"round": rnd + 1, "span": round(span, 3),
                                "combos": len(combos), "improved": improved,
                                "best_metric": best["metric"]})
                await emit_event({"type": "markets.backtest", "stage": "autotune_round",
                                  "autotune_id": aid, "round": rnd + 1,
                                  "improved": improved, "best": best["stats"],
                                  "values": best["values"]})
                span = min(0.9, span * 1.8) if not improved else max(0.05, span * 0.5)
            st["history"] = history

            # validation re-selection: the in-sample winner must also lead on a
            # held-back validation slice, else the best VALIDATION finalist wins
            # — a second line of defence against knife-edge overfits
            if len(finalists) > 1 and split_i > 500 and not st.get("cancel"):
                v0 = int(split_i * 0.75)
                arr_val = {k: v[v0:split_i] for k, v in arr_full.items()}
                best_val = None
                for f in finalists[:8]:
                    try:
                        stats_v = await evaluate(f["values"], on=arr_val)
                        fv = _mval(stats_v)
                    except Exception:
                        continue
                    if fv is not None and (best_val is None or fv > best_val["m"]):
                        best_val = {"values": f["values"], "m": fv}
                if best_val and json.dumps(best_val["values"], sort_keys=True) != \
                        json.dumps(best["values"], sort_keys=True):
                    stats_b = await evaluate(best_val["values"]) or {}
                    mb = _mval(stats_b)
                    if mb is not None:
                        best = {"values": best_val["values"], "stats": stats_b,
                                "metric": mb}
                        st["best"] = best
                        st["validation_pick"] = True

            # out-of-sample verdict on the unseen tail
            if split_i < n_all and best.get("stats"):
                arr_oos = {k: v[split_i:] for k, v in arr_full.items()}
                try:
                    oos = await evaluate(best["values"], on=arr_oos)
                    st["stats_oos"] = {k: oos.get(k) for k in
                                       list(lab.SWEEP_METRICS) + ["trades",
                                                                  "buy_hold_return_pct"]} \
                        if oos else None
                except Exception as e:
                    st["stats_oos"] = {"error": str(e)[:160]}

            # per-axis sensitivity around the winner (robust vs knife-edge)
            if st.get("sensitivity", True) and best.get("stats") and not st.get("cancel"):
                sens = []
                for ax in axes[:6]:
                    p = ax["path"]
                    bv = best["values"].get(p, ax["value"])
                    if abs(bv) < 1e-12:
                        continue
                    vlist = _axis_values(bv, is_int.get(p, False), 0.4, 7,
                                         keys.get(p, "")) or []
                    row = {"path": p, "values": [], "metric": []}
                    for v in vlist:
                        stats = await try_vals({**best["values"], p: v})
                        row["values"].append(v)
                        row["metric"].append(None if stats is None
                                             else _mval(stats))
                    if row["values"]:
                        sens.append(row)
                st["sensitivity_data"] = sens

            # persist winner as a normal backtest row
            if st["status"] == "running" and best.get("stats"):
                best_spec = apply_vals(spec, best["values"])
                entry, exit_, s_en, s_ex = await lab._spec_signals(arr_full, best_spec)
                res = await loop.run_in_executor(None, lab.run_backtest,
                                                 arr_full, entry, exit_, best_spec,
                                                 s_en, s_ex)
                eq_t, eq = res["equity_t"], res["equity"]
                if len(eq) > 600:
                    fstep = len(eq) / 600.0
                    idx = sorted({int(i * fstep) for i in range(600)} | {len(eq) - 1})
                    eq_t = [eq_t[i] for i in idx]
                    eq = [eq[i] for i in idx]
                bid = uuid.uuid4().hex[:10]

                def _ins():
                    conn = _sqlite_conn()
                    try:
                        conn.execute(
                            "INSERT INTO mkt_backtests (id,dataset_id,strategy_id,"
                            "name,spec,stats,result,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (bid, st["dataset_id"], st.get("strategy_id") or "",
                             f"{st['name']} [autotune]", json.dumps(best_spec),
                             json.dumps(res["stats"]),
                             json.dumps({"equity_t": eq_t, "equity": eq,
                                         "trades": res["trades"][-300:]}),
                             now_iso()))
                        conn.commit()
                    finally:
                        conn.close()
                await loop.run_in_executor(None, _ins)
                st["best_backtest_id"] = bid
                st["best_spec"] = best_spec
                if st.get("update_strategy") and st.get("strategy_id"):
                    row = await loop.run_in_executor(
                        None, lab._strategy_full_sync, st["strategy_id"])
                    if row:
                        await lab.cap_strategy_save(
                            name=row.get("name") or st["name"], spec=best_spec,
                            kind=best_spec.get("kind") or row.get("kind") or "rule",
                            id=st["strategy_id"])
                        st["strategy_updated"] = True
                st["status"] = "done"
            elif st["status"] == "running":
                st["status"] = "done"
            await emit_event({"type": "markets.backtest", "stage": "autotune_done",
                              "autotune_id": aid, "status": st["status"],
                              "best": (st.get("best") or {}).get("stats"),
                              "values": (st.get("best") or {}).get("values"),
                              "baseline": st.get("baseline"),
                              "stats_oos": st.get("stats_oos"),
                              "oos_bars": st.get("oos_bars"),
                              "best_backtest_id": st.get("best_backtest_id"),
                              "strategy_updated": st.get("strategy_updated", False)})
        except Exception as e:
            log.warning("autotune %s failed: %s", aid, e)
            st["status"] = "error"
            st["error"] = str(e)[:300]
            await emit_event({"type": "markets.backtest", "stage": "autotune_error",
                              "autotune_id": aid, "error": st["error"]})

    @capability(
        "markets.backtest.autotune", http_method="POST",
        http_path="/markets/backtest/autotune", http_tags=["markets"], memory="on",
        description="Deterministically auto-tune a strategy on a dataset: numeric "
                    "spec parameters are discovered automatically (or pass axes), a "
                    "coarse grid is evaluated, then the search re-centres on the "
                    "winner and zooms in over several rounds (widening again when "
                    "stuck). The winner is stored as a backtest ('[autotune]') and "
                    "can be written back to the strategy. Progress streams as "
                    "markets.backtest events (stage autotune_progress/round/done). "
                    "Input: dataset_id (str!), strategy_id (str) OR spec (object), "
                    "metric (sharpe|sortino|total_return_pct|cagr_pct|calmar|"
                    "profit_factor|win_rate_pct|expectancy_pct|max_drawdown_pct), "
                    "rounds (int=3), per_round (int=60 evals), axes (list — "
                    "[{path,label?}] override auto-discovery), update_strategy "
                    "(bool=False — write winning params back), oos_split "
                    "(float=0.25 — hold out this tail fraction: the search never "
                    "sees it, the winner is re-scored on it → stats_oos exposes "
                    "overfitting), min_trades (int=5 — reject combos with fewer "
                    "trades), explore (int=10 — extra random-jitter samples per "
                    "round to escape local optima), sensitivity (bool=True — "
                    "per-axis metric curves around the winner), limit (int=20000), "
                    "name (str). Output: {ok, autotune_id, axes, rounds, total_est}.",
    )
    async def cap_backtest_autotune(dataset_id: str = "", strategy_id: str = "",
                                    spec=None, metric: str = "sharpe",
                                    rounds: int = 3, per_round: int = 60,
                                    axes=None, update_strategy: bool = False,
                                    oos_split: float = 0.25, min_trades: int = 5,
                                    explore: int = 10, sensitivity: bool = True,
                                    limit: int = 20000, name: str = "",
                                    trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        lab = _mlab()
        if not lab:
            return {"error": "markets lab module unavailable"}
        await _ensure_tables()
        metric = (metric or "sharpe").lower()
        if metric != "blend" and metric not in lab.SWEEP_METRICS:
            return {"error": f"unknown metric '{metric}'",
                    "valid": list(lab.SWEEP_METRICS) + ["blend"]}
        loaded, arr, err = await lab._load_spec_and_arr(dataset_id, strategy_id,
                                                        spec, "", "", limit)
        if err:
            return err
        full_spec = loaded["spec"]
        discovered = discover_axes(full_spec)
        if isinstance(axes, str):
            try:
                axes = json.loads(axes)
            except Exception:
                axes = None
        if isinstance(axes, list) and axes:
            picked = []
            by_path = {ax["path"]: ax for ax in discovered}
            for a in axes[:6]:
                p = a.get("path") if isinstance(a, dict) else str(a)
                if p in by_path:
                    picked.append(by_path[p])
                else:
                    try:
                        probe = json.loads(json.dumps(full_spec))
                        cur = probe
                        for part in str(p).split("."):
                            cur = cur[int(part)] if part.lstrip("-").isdigit() and \
                                isinstance(cur, list) else cur[part]
                        picked.append({"path": p, "value": float(cur),
                                       "int": isinstance(cur, int),
                                       "key": str(p).rsplit(".", 1)[-1]})
                    except Exception:
                        return {"error": f"axis path '{p}' not found in spec"}
            discovered = picked
        if not discovered:
            return {"error": "no tunable numeric parameters found in this spec"}
        use_axes = discovered[:6]
        rounds = max(1, min(10, int(rounds)))
        per_round = max(9, min(400, int(per_round)))
        aid = uuid.uuid4().hex[:10]
        _AUTOTUNES[aid] = {
            "id": aid, "status": "running", "dataset_id": dataset_id,
            "strategy_id": strategy_id or "", "metric": metric,
            "name": name or loaded["name"] or "autotune",
            "spec": full_spec, "axes": use_axes, "rounds": rounds,
            "per_round": per_round, "update_strategy": bool(update_strategy),
            "oos_split": max(0.0, min(0.6, float(oos_split or 0))),
            "min_trades": max(0, int(min_trades)),
            "explore": max(0, min(40, int(explore))),
            "sensitivity": bool(sensitivity),
            "done": 0, "total_est": rounds * (per_round + max(0, int(explore))) + 1,
            "created_at": now_iso(), "_arr": arr,
        }
        if len(_AUTOTUNES) > 15:
            for old in sorted(_AUTOTUNES.values(),
                              key=lambda s: s["created_at"])[:-15]:
                if old["status"] != "running":
                    _AUTOTUNES.pop(old["id"], None)
        asyncio.create_task(_autotune_task(aid))
        await emit_event({"type": "markets.backtest", "stage": "autotune_start",
                          "autotune_id": aid, "dataset_id": dataset_id,
                          "metric": metric, "rounds": rounds,
                          "axes": [ax["path"] for ax in use_axes]})
        return {"ok": True, "autotune_id": aid, "metric": metric,
                "axes": [{"path": ax["path"], "value": ax["value"]} for ax in use_axes],
                "rounds": rounds, "total_est": rounds * per_round + 1}

    @capability(
        "markets.backtest.autotune_status", http_method="GET",
        http_path="/markets/backtest/autotune/status", http_tags=["markets"],
        memory="off", silent=True,
        description="Status of an autotune run. Input: id (str — omit to list), "
                    "cancel (bool). Output: {autotune:{id,status,done,total_est,"
                    "baseline,best:{values,stats,metric},history,best_backtest_id,"
                    "best_spec,strategy_updated,error}} or {autotunes:[…]}.",
    )
    async def cap_backtest_autotune_status(id: str = "", cancel: bool = False,
                                           trace_id=None) -> dict:
        if not id:
            return {"autotunes": [{k: s.get(k) for k in
                                   ("id", "status", "done", "total_est", "metric",
                                    "dataset_id", "name", "created_at")}
                                  for s in sorted(_AUTOTUNES.values(),
                                                  key=lambda s: s["created_at"],
                                                  reverse=True)]}
        st = _AUTOTUNES.get(id)
        if not st:
            return {"error": "no such autotune (kept in memory until restart)", "id": id}
        if cancel and st["status"] == "running":
            st["cancel"] = True
        out = {k: st.get(k) for k in
               ("id", "status", "done", "total_est", "metric", "dataset_id",
                "strategy_id", "name", "baseline", "best", "history",
                "stats_oos", "oos_bars", "oos_split", "min_trades",
                "sensitivity_data", "validation_pick",
                "best_backtest_id", "best_spec", "strategy_updated", "error",
                "created_at")}
        out["axes"] = [{"path": ax["path"], "value": ax["value"]}
                       for ax in (st.get("axes") or [])]
        return {"autotune": out}

    # ── Pivot points (pluggable methods, same display mechanism as trendfit) ─

    @capability(
        "markets.analysis.pivots", http_method="POST",
        http_path="/markets/analysis/pivots", http_tags=["markets"],
        memory="off", silent=True,
        schema=enum_schema(method=PIVOT_METHODS),
        description="Detect price pivot points with a pluggable method — each "
                    "returns the same shape so charts display them uniformly. "
                    "Methods: zigzag (reversal > pct%), atr_zigzag (reversal > "
                    "mult×ATR — volatility-adaptive), fractal (Williams fractals, "
                    "n bars each side), rdp (Ramer–Douglas–Peucker on log price — "
                    "detail 0-100 like trendfit). Input: dataset_id (str!), method "
                    "(str=zigzag), pct (float=5), mult (float=3), atr_n (int=14), "
                    "n (int=2), detail (int=50), limit (int=5000). Output: "
                    "{method, pivots:[{t,p,kind:high|low}], line:[{t,p}], count}.",
    )
    async def cap_pivots(dataset_id: str = "", method: str = "zigzag",
                         pct: float = 5.0, mult: float = 3.0, atr_n: int = 14,
                         n: int = 2, detail: int = 50, limit: int = 5000,
                         trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        md, ma = _md(), _ma()
        if not (md and ma):
            return {"error": "markets modules unavailable"}
        method = (method or "zigzag").lower()
        if method not in PIVOT_METHODS:
            return {"error": f"unknown method '{method}'", "valid": PIVOT_METHODS}
        bars = await md.get_bars(dataset_id, max(50, min(50_000, int(limit))))
        if len(bars) < 10:
            return {"error": f"only {len(bars)} bars stored for {dataset_id}"}
        arr = ma.bars_to_arrays(bars)

        def _run():
            t, c, h, l = arr["t"], arr["c"], arr["h"], arr["l"]
            if method == "zigzag":
                piv = zigzag_pivots(t, c, pct)
            elif method == "atr_zigzag":
                atr = ma.compute_indicator(arr, "atr", {"n": int(atr_n)})["atr"]
                piv = atr_zigzag_pivots(h, l, c, t, atr, mult)
            elif method == "fractal":
                piv = fractal_pivots(h, l, t, n)
            else:                                 # rdp
                y = np.log(np.maximum(1e-12, c))
                xn = (t - t[0]) / max(1.0, float(t[-1] - t[0]))
                yn = (y - y.min()) / max(1e-12, (y.max() - y.min()))
                idx = rdp_indices(xn.astype(np.float64), yn, detail_to_eps(detail))
                piv = []
                for j, i in enumerate(idx):
                    kind = "pivot"
                    if 0 < j < len(idx) - 1:
                        kind = "high" if c[i] >= c[idx[j - 1]] and c[i] >= c[idx[j + 1]] \
                            else ("low" if c[i] <= c[idx[j - 1]] and c[i] <= c[idx[j + 1]]
                                  else "pivot")
                    piv.append({"t": int(t[i]), "p": float(c[i]), "kind": kind})
            line = [{"t": p["t"], "p": p["p"]} for p in piv] \
                if method != "fractal" else []
            return piv, line
        piv, line = await asyncio.get_running_loop().run_in_executor(None, _run)
        return {"ok": True, "dataset_id": dataset_id, "method": method,
                "pivots": piv[-400:], "line": line[-400:], "count": len(piv)}

    # ── Multi-market batch screener ──────────────────────────────────────────

    _BATCHES: Dict[str, dict] = {}

    async def _batch_task(bid: str):
        st = _BATCHES[bid]
        lab, md, ma = _mlab(), _md(), _ma()
        loop = asyncio.get_running_loop()
        try:
            metric = st["metric"]
            arrs: Dict[str, object] = {}
            results: List[dict] = []
            total = len(st["combos"])
            for i, (strat, ds) in enumerate(st["combos"]):
                if st.get("cancel"):
                    st["status"] = "cancelled"
                    break
                if ds not in arrs:
                    bars = await md.get_bars(ds, st["limit"])
                    arrs[ds] = ma.bars_to_arrays(bars) if len(bars) >= 100 else None
                arr = arrs[ds]
                row = {"strategy_id": strat.get("id") or "",
                       "strategy": strat.get("name") or "inline",
                       "dataset_id": ds}
                if arr is None:
                    row["error"] = "not enough bars"
                else:
                    try:
                        e, x, se, sx = await lab._spec_signals(arr, strat["spec"])
                        res = await loop.run_in_executor(
                            None, lab.run_backtest, arr, e, x, strat["spec"], se, sx)
                        row["stats"] = {k: res["stats"].get(k) for k in lab.SWEEP_METRICS}
                        row["stats"]["trades"] = res["stats"].get("trades")
                        row["stats"]["buy_hold_return_pct"] = \
                            res["stats"].get("buy_hold_return_pct")
                    except Exception as e2:
                        row["error"] = str(e2)[:180]
                results.append(row)
                st["done"] = i + 1
                if (i + 1) % max(1, total // 12) == 0 or i + 1 == total:
                    await emit_event({"type": "markets.backtest",
                                      "stage": "batch_progress", "batch_id": bid,
                                      "done": i + 1, "total": total})
            valid = [r for r in results if r.get("stats") and
                     r["stats"].get(metric) is not None]
            valid.sort(key=lambda r: r["stats"][metric], reverse=True)
            st["results"] = valid + [r for r in results if r.get("error")]

            # optional autotune refinement of the top plays
            for r in valid[:st.get("autotune_top") or 0]:
                if st.get("cancel"):
                    break
                strat = next((s for s, d in st["combos"]
                              if (s.get("id") or "") == r["strategy_id"] and
                              d == r["dataset_id"]), None)
                arr = arrs.get(r["dataset_id"])
                if not (strat and arr is not None):
                    continue
                spec = json.loads(json.dumps(strat["spec"]))
                axes = discover_axes(spec)[:3]
                if not axes:
                    continue
                best_stats, best_vals = r["stats"], {}
                span = 0.4
                for rnd in range(2):
                    grids = {}
                    for ax in axes:
                        cur = best_vals.get(ax["path"], ax["value"])
                        vals = _axis_values(cur, ax["int"], span, 4, ax["key"])
                        if vals:
                            grids[ax["path"]] = vals
                    combos2: List[dict] = [{}]
                    for p, vals in grids.items():
                        combos2 = [{**cc, p: v} for cc in combos2 for v in vals]
                    for cc in combos2[:36]:
                        try:
                            s2 = json.loads(json.dumps(spec))
                            for p, v in {**best_vals, **cc}.items():
                                vv = int(v) if float(v).is_integer() else float(v)
                                lab.set_spec_path(s2, p, vv)
                            e, x, se, sx = await lab._spec_signals(arr, s2)
                            res = await loop.run_in_executor(
                                None, lab.run_backtest, arr, e, x, s2, se, sx)
                            mv = res["stats"].get(metric)
                            if mv is not None and mv > (best_stats.get(metric) or -1e18):
                                best_stats = {k: res["stats"].get(k)
                                              for k in lab.SWEEP_METRICS}
                                best_vals = {**best_vals, **cc}
                        except Exception:
                            continue
                    span *= 0.5
                if best_vals:
                    r["tuned"] = {"values": best_vals, "stats": best_stats}
                await emit_event({"type": "markets.backtest", "stage": "batch_tuned",
                                  "batch_id": bid, "strategy": r["strategy"],
                                  "dataset_id": r["dataset_id"]})
            if st["status"] == "running":
                st["status"] = "done"
            await loop.run_in_executor(
                None, _kv_set_sync, "studio:batch:last",
                {"id": bid, "metric": metric, "created_at": st["created_at"],
                 "results": st["results"][:60]})
            await emit_event({"type": "markets.backtest", "stage": "batch_done",
                              "batch_id": bid, "status": st["status"],
                              "top": st["results"][:5]})
        except Exception as e:
            log.warning("batch %s failed: %s", bid, e)
            st["status"] = "error"
            st["error"] = str(e)[:300]
            await emit_event({"type": "markets.backtest", "stage": "batch_error",
                              "batch_id": bid, "error": st["error"]})

    @capability(
        "markets.backtest.batch", http_method="POST",
        http_path="/markets/backtest/batch", http_tags=["markets"], memory="on",
        description="Screen strategies ACROSS markets: every (strategy × dataset) "
                    "combo is backtested and ranked to find the best plays; the "
                    "top results can be auto-fine-tuned (2-round zoom grid). "
                    "Input: strategy_ids (list — saved ids; or 'library' to screen "
                    "every library template), datasets (list of dataset_ids) OR "
                    "assets (list of 'provider:symbol') + tf (str=1d) OR "
                    "all_watchlist (bool) + tf, metric (str=sharpe), autotune_top "
                    "(int=0 — refine the N best), limit (int=8000 bars). ≤120 "
                    "combos. Progress via markets.backtest events (batch_*). "
                    "Output: {ok, batch_id, combos}.",
    )
    async def cap_backtest_batch(strategy_ids=None, datasets=None, assets=None,
                                 tf: str = "1d", all_watchlist: bool = False,
                                 metric: str = "sharpe", autotune_top: int = 0,
                                 limit: int = 8000, trace_id=None) -> dict:
        lab, md, mc = _mlab(), _md(), _mc()
        if not (lab and md and mc):
            return {"error": "markets modules unavailable"}
        await _ensure_tables()
        metric = (metric or "sharpe").lower()
        if metric not in lab.SWEEP_METRICS:
            return {"error": f"unknown metric '{metric}'", "valid": list(lab.SWEEP_METRICS)}
        loop = asyncio.get_running_loop()
        for name_, v in (("strategy_ids", strategy_ids), ("datasets", datasets),
                         ("assets", assets)):
            if isinstance(v, str) and v not in ("library",):
                try:
                    v = json.loads(v)
                except Exception:
                    v = [x.strip() for x in v.split(",") if x.strip()]
                if name_ == "strategy_ids":
                    strategy_ids = v
                elif name_ == "datasets":
                    datasets = v
                else:
                    assets = v
        # strategies
        strats: List[dict] = []
        if strategy_ids == "library" or (isinstance(strategy_ids, list) and
                                         "library" in strategy_ids):
            strats += [{"id": "", "name": tp["name"],
                        "spec": json.loads(json.dumps(tp["spec"]))}
                       for tp in STRATEGY_LIBRARY if not tp.get("needs_model")]
        if isinstance(strategy_ids, list):
            for sid in strategy_ids:
                if sid == "library":
                    continue
                row = await loop.run_in_executor(None, lab._strategy_full_sync, str(sid))
                if row:
                    strats.append({"id": row["id"], "name": row["name"],
                                   "spec": row["spec"]})
        if not strats:
            return {"error": "no strategies — pass strategy_ids (or 'library')"}
        # datasets
        dss: List[str] = list(datasets or [])
        if isinstance(assets, list):
            for a in assets:
                prov, sym = (a.split(":", 1) + [""])[:2] if ":" in a else ("binance", a)
                dss.append(md._dataset_id(prov, sym, tf or "1d"))
        if all_watchlist:
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            dss += [md._dataset_id(r["exchange"], r["symbol"], tf or "1d")
                    for r in rows if r.get("exchange") != "macro"]
        dss = list(dict.fromkeys(dss))
        if not dss:
            return {"error": "no datasets — pass datasets, assets or all_watchlist"}
        combos = [(s, d) for s in strats for d in dss][:120]
        bid = uuid.uuid4().hex[:10]
        _BATCHES[bid] = {"id": bid, "status": "running", "metric": metric,
                         "combos": combos, "total": len(combos), "done": 0,
                         "autotune_top": max(0, min(10, int(autotune_top))),
                         "limit": max(500, min(50_000, int(limit))),
                         "results": [], "created_at": now_iso()}
        if len(_BATCHES) > 8:
            for old in sorted(_BATCHES.values(), key=lambda s: s["created_at"])[:-8]:
                if old["status"] != "running":
                    _BATCHES.pop(old["id"], None)
        asyncio.create_task(_batch_task(bid))
        await emit_event({"type": "markets.backtest", "stage": "batch_start",
                          "batch_id": bid, "combos": len(combos),
                          "strategies": len(strats), "datasets": len(dss)})
        return {"ok": True, "batch_id": bid, "combos": len(combos),
                "strategies": len(strats), "datasets": len(dss)}

    @capability(
        "markets.backtest.batch_status", http_method="GET",
        http_path="/markets/backtest/batch/status", http_tags=["markets"],
        memory="off", silent=True,
        description="Status/leaderboard of a batch screen. Input: id (str — blank "
                    "= last finished leaderboard), top (int=40), cancel (bool). "
                    "Output: {batch:{id,status,done,total,metric,results:[…]}}.",
    )
    async def cap_backtest_batch_status(id: str = "", top: int = 40,
                                        cancel: bool = False, trace_id=None) -> dict:
        if not id:
            last = await asyncio.get_running_loop().run_in_executor(
                None, _kv_get_sync, "studio:batch:last")
            running = [{k: s.get(k) for k in ("id", "status", "done", "total",
                                              "metric", "created_at")}
                       for s in _BATCHES.values() if s["status"] == "running"]
            return {"last": last, "running": running}
        st = _BATCHES.get(id)
        if not st:
            return {"error": "no such batch (kept in memory until restart)", "id": id}
        if cancel and st["status"] == "running":
            st["cancel"] = True
        out = {k: st.get(k) for k in ("id", "status", "done", "total", "metric",
                                      "error", "created_at")}
        out["results"] = (st.get("results") or [])[:max(1, min(120, int(top)))]
        return {"batch": out}

    # ── ML walk-forward backtesting (true out-of-sample) ─────────────────────

    async def _ml_wf_task(wid: str, cfg: dict):
        lab, md, ma = _mlab(), _md(), _ma()
        loop = asyncio.get_running_loop()
        try:
            bars = await md.get_bars(cfg["dataset_id"], cfg["limit"])
            if len(bars) < 400:
                raise ValueError(f"need ≥400 bars, have {len(bars)}")
            arr = ma.bars_to_arrays(bars)
            feats = cfg["features"]
            X, names = lab.build_features(arr, feats, ma.compute_indicator)
            c = arr["c"]
            n = len(c)
            horizon = max(1, int(cfg["horizon"]))
            fwd = np.full(n, np.nan)
            fwd[:-horizon] = c[horizon:] / c[:-horizon] - 1.0
            task = cfg["task"]
            y = (fwd > 0).astype(int) if task == "classify" else fwd
            valid = ~np.isnan(fwd) & ~np.any(np.isnan(X), axis=1)
            iv = np.where(valid)[0]
            if len(iv) < 400:
                raise ValueError(f"only {len(iv)} usable samples")
            folds = max(2, min(12, int(cfg["folds"])))
            warm = int(len(iv) * 0.3)
            seg = max(30, (len(iv) - warm) // folds)
            sig = np.full(n, np.nan)
            for k in range(folds):
                a = warm + k * seg
                b = len(iv) if k == folds - 1 else min(len(iv), a + seg)
                if a >= len(iv) or a >= b:
                    break
                tr_idx, te_idx = iv[:a], iv[a:b]

                def _fit(tr_idx=tr_idx, te_idx=te_idx):
                    m = lab._make_model(task, cfg["model_kind"], cfg["hyperparams"])
                    m.fit(X[tr_idx], y[tr_idx])
                    if task == "classify" and hasattr(m, "predict_proba"):
                        return m.predict_proba(X[te_idx])[:, 1]
                    return m.predict(X[te_idx])
                pred = await loop.run_in_executor(None, _fit)
                sig[te_idx] = pred
                await emit_event({"type": "markets.ml", "stage": "wf_progress",
                                  "id": wid, "fold": k + 1, "folds": folds,
                                  "train": int(len(tr_idx)), "test": int(len(te_idx))})
            ok = ~np.isnan(sig)
            ea = float(cfg["enter_above"])
            xb = float(cfg["exit_below"])
            entry = ok & (sig > ea)
            exit_ = ok & (sig < xb)
            s_en = s_ex = None
            if cfg.get("short_below") is not None:
                sb = float(cfg["short_below"])
                s_en = ok & (sig < sb)
                s_ex = ok & (sig > float(cfg.get("short_exit_above", sb + 0.2)))
            opts = {"fee_bps": cfg.get("fee_bps", 10),
                    "slippage_bps": cfg.get("slippage_bps", 5)}
            res = await loop.run_in_executor(None, lab.run_backtest,
                                             arr, entry, exit_, opts, s_en, s_ex)
            stats = res["stats"]
            stats["engine"] = "ml-walkforward"
            stats["folds"] = folds
            stats["oos_bars"] = int(ok.sum())
            eq_t, eq = res["equity_t"], res["equity"]
            if len(eq) > 600:
                fstep = len(eq) / 600.0
                idx = sorted({int(i * fstep) for i in range(600)} | {len(eq) - 1})
                eq_t = [eq_t[i] for i in idx]
                eq = [eq[i] for i in idx]
            spec = {"kind": "ml-walkforward", **{k: cfg[k] for k in
                    ("task", "model_kind", "features", "horizon", "hyperparams",
                     "folds", "enter_above", "exit_below")}}

            def _ins():
                conn = _sqlite_conn()
                try:
                    conn.execute(
                        "INSERT INTO mkt_backtests (id,dataset_id,strategy_id,name,"
                        "spec,stats,result,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (wid, cfg["dataset_id"], "", cfg["name"], json.dumps(spec),
                         json.dumps(stats),
                         json.dumps({"equity_t": eq_t, "equity": eq,
                                     "trades": res["trades"][-300:]}),
                         now_iso()))
                    conn.commit()
                finally:
                    conn.close()
            await loop.run_in_executor(None, _ins)
            await emit_event({"type": "markets.backtest", "stage": "done",
                              "id": wid, "dataset_id": cfg["dataset_id"],
                              "name": cfg["name"], "stats": stats})
        except Exception as e:
            log.warning("ml walkforward %s: %s", wid, e)
            await emit_event({"type": "markets.ml", "stage": "wf_error",
                              "id": wid, "error": str(e)[:300]})

    @capability(
        "markets.ml.walkforward", http_method="POST",
        http_path="/markets/ml/walkforward", http_tags=["markets"], memory="on",
        description="TRUE out-of-sample ML backtest: the model is retrained on an "
                    "expanding window and only ever predicts bars it has never "
                    "seen (per fold), then the stitched out-of-sample signal is "
                    "traded through the native engine — no lookahead, unlike "
                    "backtesting a fully-trained model. Result lands as a normal "
                    "backtest row (engine 'ml-walkforward'). Input: dataset_id "
                    "(str!), model_id (str — copy config from a saved ML tool) OR "
                    "task/model_kind/features/horizon/hyperparams (as "
                    "markets.ml.create), folds (int=5), enter_above (float=0.55), "
                    "exit_below (float=0.45), short_below (float — optional "
                    "short side), limit (int=20000), name (str). Output: {ok, id}.",
    )
    async def cap_ml_walkforward(dataset_id: str = "", model_id: str = "",
                                 task: str = "classify", model_kind: str = "gbt",
                                 features=None, horizon: int = 5, hyperparams=None,
                                 folds: int = 5, enter_above: float = 0.55,
                                 exit_below: float = 0.45, short_below: float = None,
                                 limit: int = 20000, name: str = "",
                                 trace_id=None) -> dict:
        lab = _mlab()
        if not lab:
            return {"error": "markets lab module unavailable"}
        if not getattr(lab, "HAS_SKLEARN", False):
            return {"error": "scikit-learn not installed"}
        if not dataset_id:
            return {"error": "dataset_id required"}
        await _ensure_tables()
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except Exception:
                features = [f.strip() for f in features.split(",") if f.strip()]
        if isinstance(hyperparams, str):
            try:
                hyperparams = json.loads(hyperparams)
            except Exception:
                hyperparams = {}
        if model_id:
            row = await asyncio.get_running_loop().run_in_executor(
                None, lab._ml_row_sync, model_id)
            if not row:
                return {"error": "no such ML model", "model_id": model_id}
            task = row["task"]
            model_kind = row["model_kind"]
            horizon = row["horizon"]
            try:
                features = json.loads(row["features"] or "[]")
            except Exception:
                features = None
            try:
                hyperparams = json.loads(row["hyperparams"] or "{}")
            except Exception:
                hyperparams = {}
            name = name or f"{row.get('name')} walk-forward"
        wid = uuid.uuid4().hex[:10]
        cfg = {"dataset_id": dataset_id, "task": task, "model_kind": model_kind,
               "features": features or lab.DEFAULT_FEATURES,
               "horizon": max(1, int(horizon)),
               "hyperparams": hyperparams or {}, "folds": int(folds),
               "enter_above": float(enter_above), "exit_below": float(exit_below),
               "short_below": short_below,
               "limit": max(500, min(100_000, int(limit))),
               "name": name or "ML walk-forward"}
        asyncio.create_task(_ml_wf_task(wid, cfg))
        await emit_event({"type": "markets.ml", "stage": "wf_start", "id": wid,
                          "dataset_id": dataset_id, "folds": int(folds)})
        return {"ok": True, "id": wid, "name": cfg["name"]}

    # ── On-the-spot infographics (agent-buildable, live-rendered) ────────────

    INFOG_PANEL_TYPES = ["stat", "spark", "bars", "donut", "gauge", "heatmap", "text"]

    @capability(
        "markets.infographic.save", http_method="POST",
        http_path="/markets/infographic/save", http_tags=["markets"], memory="on",
        description="Build/update a live infographic that renders instantly in the "
                    "Quant Studio Pulse tab — THE way for agents to compose custom "
                    "visuals on the spot from any data they've gathered. Input: "
                    "name (str!), spec (object!: {title, subtitle, panels:[≤12 of "
                    "{type: stat|spark|bars|donut|gauge|heatmap|text, label (str), "
                    "value (str|num — stat/gauge headline), delta (num — signed % "
                    "shown coloured), data ([num] for spark/bars/donut/gauge-pct; "
                    "[[num]] rows for heatmap), labels ([str] — bars/donut/heatmap "
                    "axes), color (css), text (str — for type text), wide (bool)}]}), "
                    "id (str — update existing), author (str='vera'). "
                    "Output: {ok, id}.",
    )
    async def cap_infographic_save(name: str = "", spec=None, id: str = "",
                                   author: str = "vera", trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                return {"error": "spec must be a JSON object"}
        if not isinstance(spec, dict) or not isinstance(spec.get("panels"), list) \
                or not spec["panels"]:
            return {"error": "spec.panels:[…] required"}
        panels = []
        for p in spec["panels"][:12]:
            if not isinstance(p, dict):
                continue
            ty = str(p.get("type") or "stat")
            if ty not in INFOG_PANEL_TYPES:
                return {"error": f"unknown panel type '{ty}'",
                        "valid": INFOG_PANEL_TYPES}
            panels.append(p)
        if not panels:
            return {"error": "no valid panels"}
        spec["panels"] = panels
        await _ensure_tables()
        iid = id or ("ig_" + uuid.uuid4().hex[:8])
        await asyncio.get_running_loop().run_in_executor(
            None, _kv_set_sync, f"studio:infog:{iid}",
            {"name": name.strip(), "spec": spec, "author": author or "vera"})
        await emit_event({"type": "markets.infographic", "stage": "saved",
                          "id": iid, "name": name.strip(), "author": author})
        return {"ok": True, "id": iid}

    @capability(
        "markets.infographic.list", http_method="GET",
        http_path="/markets/infographic/list", http_tags=["markets"],
        memory="off", silent=True,
        description="List saved infographics. Output: {infographics:[{id,name,"
                    "spec,author,updated_at}]}.",
    )
    async def cap_infographic_list(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _kv_scan_sync, "studio:infog:")
        out = [{"id": r["key"].split(":", 2)[2], "updated_at": r["updated_at"],
                **(r["value"] or {})} for r in rows]
        return {"infographics": out, "count": len(out)}

    @capability(
        "markets.infographic.delete", http_method="POST",
        http_path="/markets/infographic/delete", http_tags=["markets"], memory="on",
        description="Delete an infographic. Input: id (str!). Output: {ok}.",
    )
    async def cap_infographic_delete(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        await asyncio.get_running_loop().run_in_executor(
            None, _kv_del_sync, f"studio:infog:{id}")
        await emit_event({"type": "markets.infographic", "stage": "deleted", "id": id})
        return {"ok": True, "id": id}

    # ── Market dynamics & OSINT (open longs/shorts, funding, WSB alpha, news) ─

    DYN_METRICS = {
        "funding": {"name": "Funding rate", "unit": "%/8h"},
        "oi":      {"name": "Open interest", "unit": "contracts"},
        "ls_acct": {"name": "Long/Short accounts", "unit": "ratio"},
        "ls_top":  {"name": "Top-trader L/S positions", "unit": "ratio"},
    }

    def _fapi_sym(symbol: str) -> str:
        """'BTC/USDT' | 'BTC-USD' | 'btc' → Binance futures symbol 'BTCUSDT'."""
        s = "".join(ch for ch in str(symbol).upper() if ch.isalnum())
        if s.endswith("USDT"):
            return s
        if s.endswith("USD"):
            return s + "T"
        return s + "USDT"

    def _dyn_rows_sync(pair: str, metric: str) -> List[list]:
        fs = _fapi_sym(pair)
        if metric == "funding":
            j = _http_json_sync("https://fapi.binance.com/fapi/v1/fundingRate",
                                {"symbol": fs, "limit": 1000})
            out = []
            for x in (j or []):
                if not x.get("fundingTime"):
                    continue
                r = float(x.get("fundingRate") or 0) * 100
                out.append([int(x["fundingTime"]), r, r, r, r, 0.0])
            return out
        url = {"oi": "https://fapi.binance.com/futures/data/openInterestHist",
               "ls_acct": "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
               "ls_top": "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
               }.get(metric)
        if not url:
            raise ValueError(f"unknown dynamics metric '{metric}'")
        j = _http_json_sync(url, {"symbol": fs, "period": "1d", "limit": 500})
        out = []
        for x in (j or []):
            ts = int(x.get("timestamp") or 0)
            v = float(x.get("sumOpenInterest") or x.get("longShortRatio") or 0)
            if ts:
                out.append([ts, v, v, v, v,
                            float(x.get("longAccount") or 0) * 100])
        return out

    async def _dyn_ingest_timeframe(job: dict, symbol: str, tf: str,
                                    full: bool) -> int:
        """PROVIDER_INGESTORS['dyn'] — symbol encodes 'PAIR#metric' so each
        positioning series rides the shared job runner + auto-update scheduler
        and lands as an ordinary dataset (chartable layer + backtest operand)."""
        md = _md()
        pair, _, metric = symbol.partition("#")
        metric = metric or "funding"
        ds = md._dataset_id("dyn", symbol, "1d")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, md._ensure_dataset_sync, ds, "dyn",
                                   "1d", ["dynamics", metric])
        last_ms = await loop.run_in_executor(None, md._last_bar_ms_sync, ds)
        try:
            rows = await loop.run_in_executor(None, _dyn_rows_sync, pair, metric)
        except Exception as e:
            job["errors"][tf] = str(e)[:300]
            return 0
        floor = -1 if (full or last_ms is None) else last_ms
        n = await md._write_bars(ds, "dyn", symbol, "1d", rows, floor_ms=floor)
        job["fetched"][tf] = n
        job["stage"] = f"{symbol}: {n} points"
        await loop.run_in_executor(None, md._reconcile_count_sync, ds)
        await emit_event({"type": "markets.fetch", "job_id": job.get("job_id"),
                          "stage": "progress", "exchange": "dyn",
                          "symbol": symbol, "timeframe": "1d", "fetched": n})
        return n

    def _register_dyn_provider():
        md = _md()
        if md is not None and hasattr(md, "PROVIDER_INGESTORS"):
            md.PROVIDER_INGESTORS["dyn"] = _dyn_ingest_timeframe
    _register_dyn_provider()

    @capability(
        "markets.dynamics.fetch", http_method="POST",
        http_path="/markets/dynamics/fetch", http_tags=["markets"], memory="on",
        description="Fetch market-DYNAMICS series for a crypto asset from Binance "
                    "futures (keyless): funding rate history, open interest, "
                    "global long/short ACCOUNT ratio and top-trader long/short "
                    "POSITION ratio — the open-shorts/open-longs picture. Each "
                    "lands as a dataset 'mkt.dyn.<pair>_<metric>.1d' that charts "
                    "can layer and backtests can reference as a {dataset: …} "
                    "operand; with track=true they auto-refresh via the watchlist. "
                    "Input: symbol (str! e.g. 'BTC/USDT' or 'BTC-USD'), metrics "
                    "(list — funding|oi|ls_acct|ls_top; default all), track "
                    "(bool=True). Output: {ok, job_ids, datasets}.",
    )
    async def cap_dynamics_fetch(symbol: str = "", metrics=None, track: bool = True,
                                 trace_id=None) -> dict:
        mc, md = _mc(), _md()
        if not (mc and md):
            return {"error": "markets modules unavailable"}
        if not symbol:
            return {"error": "symbol required (e.g. 'BTC/USDT')"}
        _register_dyn_provider()
        if isinstance(metrics, str):
            metrics = [m.strip() for m in metrics.split(",") if m.strip()]
        want = [m for m in (metrics or list(DYN_METRICS)) if m in DYN_METRICS]
        job_ids, datasets = [], []
        for m in want:
            dsym = f"{symbol}#{m}"
            job = mc._new_job("dyn", dsym, ["1d"], True)
            job_ids.append(job["job_id"])
            datasets.append(md._dataset_id("dyn", dsym, "1d"))
            asyncio.create_task(mc._ingest_job(job["job_id"], "dyn", dsym,
                                               ["1d"], True))
            if track:
                try:
                    await mc.cap_markets_watchlist_add(
                        exchange="dyn", symbol=dsym, timeframes=["1d"],
                        auto_update=True, update_interval_min=360, backfill=False)
                except Exception:
                    pass
        await emit_event({"type": "markets.osint", "stage": "dynamics_fetch",
                          "symbol": symbol, "metrics": want})
        return {"ok": True, "job_ids": job_ids, "datasets": datasets}

    @capability(
        "markets.dynamics.snapshot", http_method="GET",
        http_path="/markets/dynamics/snapshot", http_tags=["markets"],
        memory="off", silent=True,
        description="LIVE positioning snapshot for a crypto asset (Binance "
                    "futures, no key): current funding rate, mark price, open "
                    "interest, % of accounts long vs short, and top-trader "
                    "long/short positioning. Input: symbol (str!). Output: "
                    "{symbol, funding_pct_8h, mark_price, open_interest, "
                    "accounts:{long_pct,short_pct,ratio}, "
                    "top_traders:{long_pct,short_pct,ratio}}.",
    )
    async def cap_dynamics_snapshot(symbol: str = "", trace_id=None) -> dict:
        if not symbol:
            return {"error": "symbol required"}
        fs = _fapi_sym(symbol)
        loop = asyncio.get_running_loop()

        def _snap():
            out = {"symbol": symbol, "fapi_symbol": fs}
            prem = _http_json_sync("https://fapi.binance.com/fapi/v1/premiumIndex",
                                   {"symbol": fs})
            out["funding_pct_8h"] = round(float(prem.get("lastFundingRate") or 0) * 100, 4)
            out["mark_price"] = float(prem.get("markPrice") or 0)
            oi = _http_json_sync("https://fapi.binance.com/fapi/v1/openInterest",
                                 {"symbol": fs})
            out["open_interest"] = float(oi.get("openInterest") or 0)
            for key, url in (("accounts",
                              "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"),
                             ("top_traders",
                              "https://fapi.binance.com/futures/data/topLongShortPositionRatio")):
                try:
                    j = _http_json_sync(url, {"symbol": fs, "period": "1h", "limit": 1})
                    if j:
                        lp = float(j[-1].get("longAccount") or 0) * 100
                        out[key] = {"long_pct": round(lp, 2),
                                    "short_pct": round(100 - lp, 2),
                                    "ratio": round(float(j[-1].get("longShortRatio") or 0), 3)}
                except Exception:
                    out[key] = None
            return out
        try:
            return await loop.run_in_executor(None, _snap)
        except Exception as e:
            return {"error": f"binance futures unavailable for {fs}: {e}"}

    _WSB_STOP = {
        "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HER",
        "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW",
        "NOW", "NEW", "OLD", "SEE", "WAY", "WHO", "DID", "ITS", "LET", "PUT",
        "TOO", "USE", "DD", "YOLO", "CEO", "CFO", "IPO", "ETF", "USA", "USD",
        "GDP", "CPI", "FED", "SEC", "FDA", "AI", "IMO", "TLDR", "WSB", "ATH",
        "APE", "MOON", "HODL", "FOMO", "EDIT", "LOL", "WTF", "EPS", "PE",
        "IV", "OTM", "ITM", "BUY", "SELL", "HOLD", "CALL", "PUTS", "GAIN",
        "LOSS", "BULL", "BEAR", "PSA", "IRS", "UK", "EU", "IT", "OR", "ON",
        "BE", "TO", "IN", "IS", "AT", "SO", "UP", "GO", "NO", "OK", "US",
    }
    _WSB_KNOWN = {
        "GME", "AMC", "TSLA", "AAPL", "NVDA", "AMD", "MSFT", "META", "AMZN",
        "GOOG", "GOOGL", "NFLX", "PLTR", "SOFI", "COIN", "HOOD", "RIVN",
        "LCID", "NIO", "BABA", "INTC", "MU", "SMCI", "ARM", "AVGO", "QCOM",
        "DIS", "BA", "F", "GM", "T", "VZ", "PFE", "MRNA", "SPY", "QQQ",
        "IWM", "VIX", "TLT", "GLD", "BTC", "ETH", "SOL", "DOGE", "XRP",
        "ADA", "SHIB", "PEPE", "LINK", "AVAX", "MSTR", "CRM", "ORCL", "UBER",
        "ABNB", "SHOP", "SQ", "PYPL", "ROKU", "SNAP", "CRWD", "NET", "DDOG",
        "SNOW", "U", "RBLX", "DKNG", "CHWY", "CVNA", "UPST", "AFRM", "BYND",
        "TLRY", "RKT", "BB", "NOK", "CLOV", "WISH", "SPCE", "TQQQ", "SQQQ",
    }

    def _reddit_posts_sync(sub: str, listing: str = "hot", limit: int = 100):
        j = _http_json_sync(f"https://www.reddit.com/r/{sub}/{listing}.json",
                            {"limit": min(100, limit), "raw_json": 1})
        return [c.get("data") or {} for c in
                ((j or {}).get("data") or {}).get("children") or []]

    @capability(
        "markets.wsb.scan", http_method="POST", http_path="/markets/wsb/scan",
        http_tags=["markets"], memory="on",
        description="WSB-style alpha scan: pulls hot posts from retail-trading "
                    "subreddits, extracts ticker mentions (watchlist symbols + a "
                    "known-ticker set, noise-word filtered), scores them by "
                    "mentions × upvote weight, and stores the top tickers as "
                    "DAILY series 'mkt.dyn.wsb_<ticker>.1d' so social buzz can be "
                    "charted as a layer and used in backtests ({dataset:…} "
                    "operand). Input: subs (list=['wallstreetbets','stocks',"
                    "'CryptoCurrency']), store (bool=True). Output: {ranking:"
                    "[{ticker,score,mentions,top_post}], scanned_posts, asof}.",
    )
    async def cap_wsb_scan(subs=None, store: bool = True, trace_id=None) -> dict:
        mc, md = _mc(), _md()
        loop = asyncio.get_running_loop()
        if isinstance(subs, str):
            subs = [s.strip() for s in subs.split(",") if s.strip()]
        subs = subs or ["wallstreetbets", "stocks", "CryptoCurrency"]
        tickers = set(_WSB_KNOWN)
        try:
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            for r in rows:
                base = str(r.get("symbol") or "").split("/")[0].split("-")[0].upper()
                if 1 < len(base) <= 5 and base.isalpha():
                    tickers.add(base)
        except Exception:
            pass
        import re as _re
        pat = _re.compile(r"\$?([A-Z]{2,5})\b")
        scores: Dict[str, dict] = {}
        scanned = 0
        for sub in subs[:4]:
            try:
                posts = await loop.run_in_executor(None, _reddit_posts_sync, sub)
            except Exception as e:
                log.debug("reddit %s: %s", sub, e)
                continue
            for p in posts:
                scanned += 1
                text = f"{p.get('title') or ''} {p.get('selftext') or ''}"[:3000]
                ups = max(0, int(p.get("ups") or 0))
                w = 1.0 + math.log1p(ups)
                seen_here = set()
                for m in pat.finditer(text):
                    tk = m.group(1)
                    if tk in _WSB_STOP or tk not in tickers or tk in seen_here:
                        continue
                    seen_here.add(tk)
                    d = scores.setdefault(tk, {"ticker": tk, "score": 0.0,
                                               "mentions": 0, "ups": 0,
                                               "top_post": "", "top_ups": -1})
                    d["score"] += w
                    d["mentions"] += 1
                    d["ups"] += ups
                    if ups > d["top_ups"]:
                        d["top_ups"] = ups
                        d["top_post"] = (p.get("title") or "")[:140]
        ranking = sorted(scores.values(), key=lambda d: -d["score"])[:25]
        for d in ranking:
            d["score"] = round(d["score"], 1)
            d.pop("top_ups", None)
        await loop.run_in_executor(None, _kv_set_sync, "studio:wsb:last",
                                   {"ranking": ranking, "scanned": scanned,
                                    "subs": subs})
        if store and ranking and md:
            now_ms = int(time.time() * 1000)
            for d in ranking[:10]:
                ds = md._dataset_id("dyn", f"wsb_{d['ticker']}", "1d")
                await loop.run_in_executor(None, md._ensure_dataset_sync, ds,
                                           "dyn", "1d", ["osint", "wsb"])
                await md._write_bars(ds, "dyn", f"wsb_{d['ticker']}", "1d",
                                     [[now_ms, d["score"], d["score"], d["score"],
                                       d["score"], d["mentions"]]], floor_ms=-1)
        await emit_event({"type": "markets.osint", "stage": "wsb_scan",
                          "top": [d["ticker"] for d in ranking[:5]],
                          "scanned": scanned})
        return {"ok": True, "ranking": ranking, "scanned_posts": scanned,
                "asof": now_iso()}

    @capability(
        "markets.news.feed", http_method="POST", http_path="/markets/news/feed",
        http_tags=["markets"], memory="off", silent=True,
        description="Fresh news headlines for one asset or the whole market (via "
                    "web search), cached for the dashboard. map_to_chart pins the "
                    "top headline to the asset's chart as a key-date flag. Input: "
                    "symbol_key (str — blank = whole market), query (str — "
                    "override), limit (int=8), map_to_chart (bool=False). "
                    "Output: {headlines:[{title,snippet,url}], query, cached_at}.",
    )
    async def cap_news_feed(symbol_key: str = "", query: str = "", limit: int = 8,
                            map_to_chart: bool = False, trace_id=None) -> dict:
        ma = _ma()
        q = (query or "").strip()
        if not q:
            q = ma.sentiment_query_for(symbol_key, "") if symbol_key \
                else "stock market crypto today biggest moves news"
        headlines = []
        web = sys.modules.get("web_capabilities")
        if web and hasattr(web, "cap_web_search"):
            try:
                r = await web.cap_web_search(query=q, limit=max(4, min(15, int(limit))),
                                             discover="off")
                for it in (r or {}).get("results", []):
                    t2 = (it.get("title") or "").strip()
                    if t2:
                        headlines.append({"title": t2[:200],
                                          "snippet": (it.get("snippet") or "")[:280],
                                          "url": it.get("url", "")})
            except Exception as e:
                return {"error": f"web search failed: {e}"}
        if not headlines:
            return {"error": "no headlines found (web search unavailable?)", "query": q}
        slugk = "market" if not symbol_key else symbol_key.replace(":", "_").replace("/", "_")
        await asyncio.get_running_loop().run_in_executor(
            None, _kv_set_sync, f"studio:news:{slugk}",
            {"headlines": headlines[:12], "query": q})
        if map_to_chart and symbol_key:
            await ma.cap_annotate_add(
                symbol_key=symbol_key, kind="vline",
                points=[{"t": int(time.time())}],
                text="📰 " + headlines[0]["title"][:80],
                color="#7aa2f7", author="news",
                meta={"url": headlines[0].get("url", "")})
        return {"ok": True, "query": q, "headlines": headlines[:12],
                "cached_at": now_iso()}

    @capability(
        "markets.sentiment.to_series", http_method="POST",
        http_path="/markets/sentiment/to_series", http_tags=["markets"], memory="on",
        description="Materialise an asset's LLM sentiment history (-1…+1 scores "
                    "from markets.sentiment.analyze) as a dataset "
                    "'mkt.dyn.sent_<slug>.1d' — layerable on charts and usable in "
                    "backtests as a {dataset:…} operand (e.g. entry condition "
                    "'sentiment > 0.3'). Input: symbol_key (str!). "
                    "Output: {ok, dataset_id, points}.",
    )
    async def cap_sentiment_to_series(symbol_key: str = "", trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        md = _md()
        loop = asyncio.get_running_loop()

        def _rows():
            conn = _sqlite_conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT score, created_at FROM mkt_sentiment WHERE key=? "
                    "ORDER BY created_at", (symbol_key,)).fetchall()]
            finally:
                conn.close()
        rows = await loop.run_in_executor(None, _rows)
        if not rows:
            return {"error": f"no sentiment history for {symbol_key} — run "
                             "markets.sentiment.analyze first"}
        bars = []
        for r in rows:
            ts = md._iso_to_ms(r.get("created_at") or "")
            if ts:
                v = float(r.get("score") or 0)
                bars.append([ts, v, v, v, v, 0.0])
        sl = f"sent_{symbol_key.replace(':', '_').replace('/', '_')}"
        ds = md._dataset_id("dyn", sl, "1d")
        await loop.run_in_executor(None, md._ensure_dataset_sync, ds, "dyn",
                                   "1d", ["osint", "sentiment"])
        n = await md._write_bars(ds, "dyn", sl, "1d", bars, floor_ms=-1)
        await loop.run_in_executor(None, md._reconcile_count_sync, ds)
        return {"ok": True, "dataset_id": ds, "points": n}

    # ── Sim portfolio templates ──────────────────────────────────────────────

    SIM_TEMPLATES: List[dict] = [
        {"id": "balanced-6040", "name": "Balanced 60/40", "cash": 100_000,
         "desc": "Classic diversified core: equities + long bonds + gold.",
         "holdings": [{"symbol_key": "yahoo:SPY", "weight_pct": 40},
                      {"symbol_key": "yahoo:QQQ", "weight_pct": 15},
                      {"symbol_key": "yahoo:TLT", "weight_pct": 30},
                      {"symbol_key": "yahoo:GC=F", "weight_pct": 10}]},
        {"id": "all-weather", "name": "All-Weather", "cash": 100_000,
         "desc": "Risk-balanced across growth, rates and inflation regimes.",
         "holdings": [{"symbol_key": "yahoo:SPY", "weight_pct": 30},
                      {"symbol_key": "yahoo:TLT", "weight_pct": 40},
                      {"symbol_key": "yahoo:GC=F", "weight_pct": 15}]},
        {"id": "crypto-degen", "name": "Crypto Degen", "cash": 50_000,
         "desc": "High-octane crypto majors, 20% dry powder for dips.",
         "holdings": [{"symbol_key": "yahoo:BTC-USD", "weight_pct": 50},
                      {"symbol_key": "yahoo:ETH-USD", "weight_pct": 30}]},
        {"id": "sector-rotator", "name": "Sector Rotator", "cash": 100_000,
         "desc": "Equal sector sleeves + 40% cash for the rotation scanner "
                 "and strategies to deploy.",
         "holdings": [{"symbol_key": "yahoo:XLK", "weight_pct": 15},
                      {"symbol_key": "yahoo:XLE", "weight_pct": 15},
                      {"symbol_key": "yahoo:XLF", "weight_pct": 15},
                      {"symbol_key": "yahoo:XLV", "weight_pct": 15}]},
        {"id": "strategy-sandbox", "name": "Strategy Sandbox", "cash": 100_000,
         "desc": "Pure cash — link monitored strategies or let agent loops "
                 "trade it from a clean slate.",
         "holdings": []},
    ]

    @capability(
        "markets.sim.templates", http_method="GET",
        http_path="/markets/sim/templates", http_tags=["markets"],
        memory="off", silent=True,
        description="Sim-portfolio templates (profiles) for one-click paper "
                    "accounts: Balanced 60/40, All-Weather, Crypto Degen, Sector "
                    "Rotator, Strategy Sandbox. Pass a template id to "
                    "markets.sim.create to seed the account. Output: "
                    "{templates:[{id,name,desc,cash,holdings}]}.",
    )
    async def cap_sim_templates(trace_id=None) -> dict:
        return {"templates": SIM_TEMPLATES, "count": len(SIM_TEMPLATES)}

    # ── Projections (asset + portfolio), optimizer & rotation ────────────────

    def _mc_project(value: float, mu: float, sigma: float, days: int,
                    n_paths: int = 500, seed: int = 7):
        """GBM Monte-Carlo → (t_days, {p10..p90}, paths). Weekly steps."""
        weeks = max(2, min(520, int(days // 7) or 2))
        dt = (days / 365.0) / weeks
        rng = np.random.default_rng(seed)
        z = rng.standard_normal((n_paths, weeks))
        logp = np.cumsum((mu - 0.5 * sigma * sigma) * dt +
                         sigma * math.sqrt(dt) * z, axis=1)
        paths = value * np.exp(logp)
        t_days = [int(round((k + 1) * days / weeks)) for k in range(weeks)]
        bands = {f"p{q}": [round(float(x), 2) for x in
                           np.percentile(paths, q, axis=0)]
                 for q in (10, 25, 50, 75, 90)}
        return t_days, bands, paths

    def _real_bands(bands: dict, t_days: List[int], infl_pct: float) -> dict:
        f = [(1.0 + infl_pct / 100.0) ** (d / 365.0) for d in t_days]
        return {k: [round(v / f[i], 2) for i, v in enumerate(vals)]
                for k, vals in bands.items()}

    async def _inflation_pct(default: float = 3.0) -> (float, str):
        """Latest YoY inflation from the fetched FRED layer, else the default."""
        md = _md()
        try:
            ds = md._dataset_id("macro", "fred:CPI_YOY", "1d")
            bars = await md.get_bars(ds, 3)
            if bars:
                v = float(bars[-1].get("close") or 0)
                if -5 < v < 50:
                    return v, "fred:CPI_YOY"
        except Exception:
            pass
        return float(default), "assumed"

    async def _hist_mu_sigma(ds: str, lookback: int = 1095):
        md, ma, lab = _md(), _ma(), _mlab()
        bars = await md.get_bars(ds, lookback)
        if len(bars) < 60:
            return None
        arr = ma.bars_to_arrays(bars)
        c, t = arr["c"], arr["t"]
        rets = np.diff(np.log(np.maximum(1e-12, c)))
        bpy = lab.infer_bars_per_year(t)
        return {"mu": float(np.mean(rets)) * bpy,
                "sigma": float(np.std(rets)) * math.sqrt(bpy),
                "last": float(c[-1]), "bars": len(c),
                "day_rets": {int(t[i + 1] // 86400): float(rets[i])
                             for i in range(len(rets))}}

    async def _backtest_mu_sigma(bt_id: str):
        """Annualised return/vol of a stored backtest's equity — the 'strategy
        performance' input to projections (fees/slippage already inside)."""
        lab = _mlab()
        row = await lab.cap_backtest_get(id=bt_id)
        if row.get("error"):
            return None
        eq_t = row.get("equity_t") or []
        eq = row.get("equity") or []
        if len(eq) < 20:
            return None
        e = np.asarray(eq, dtype=np.float64)
        rets = np.diff(np.log(np.maximum(1e-12, e)))
        bpy = lab.infer_bars_per_year(np.asarray(eq_t, dtype=np.int64))
        return {"mu": float(np.mean(rets)) * bpy,
                "sigma": float(np.std(rets)) * math.sqrt(bpy),
                "name": row.get("name")}

    @capability(
        "markets.project.asset", http_method="POST",
        http_path="/markets/project/asset", http_tags=["markets"],
        memory="off", silent=True,
        description="Project an asset's price into the future: Monte-Carlo GBM "
                    "bands (p10/p25/p50/p75/p90) from its historical drift+vol, or "
                    "from a STRATEGY's backtested performance (fees included) via "
                    "strategy_backtest_id. Returns nominal AND inflation-adjusted "
                    "real terms (quoted CPI YoY from the macro layer when fetched, "
                    "else inflation_pct). Input: dataset_id (str!), horizon_days "
                    "(int=365), strategy_backtest_id (str), value (float — start "
                    "value; default last price), inflation_pct (float — override "
                    "'on the ground' rate). Output: {mode, mu_annual_pct, "
                    "sigma_annual_pct, inflation:{pct,source}, t_days, "
                    "nominal:{p10..p90}, real:{p10..p90}}.",
    )
    async def cap_project_asset(dataset_id: str = "", horizon_days: int = 365,
                                strategy_backtest_id: str = "", value: float = 0.0,
                                inflation_pct: float = None, trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        hist = await _hist_mu_sigma(dataset_id)
        if not hist:
            return {"error": f"not enough bars in {dataset_id}"}
        mode, mu, sigma = "history", hist["mu"], hist["sigma"]
        if strategy_backtest_id:
            bt = await _backtest_mu_sigma(strategy_backtest_id)
            if not bt:
                return {"error": "backtest not found / too short",
                        "id": strategy_backtest_id}
            mode, mu, sigma = f"strategy:{bt['name']}", bt["mu"], bt["sigma"]
        start = float(value) if value and float(value) > 0 else hist["last"]
        days = max(30, min(3650, int(horizon_days)))
        infl, infl_src = (float(inflation_pct), "override") \
            if inflation_pct is not None else await _inflation_pct()
        t_days, bands, _ = await asyncio.get_running_loop().run_in_executor(
            None, _mc_project, start, mu, sigma, days)
        return {"dataset_id": dataset_id, "mode": mode,
                "start_value": round(start, 4),
                "mu_annual_pct": round((math.exp(mu) - 1) * 100, 2),
                "sigma_annual_pct": round(sigma * 100, 2),
                "inflation": {"pct": round(infl, 2), "source": infl_src},
                "horizon_days": days, "t_days": t_days,
                "nominal": bands, "real": _real_bands(bands, t_days, infl)}

    async def _resolve_holdings(source: str, allocations=None):
        """→ [{symbol_key, value}], cash. source: 'portfolio' | 'sim:<id>' | ''."""
        lab = _mlab()
        cash = 0.0
        out: List[dict] = []
        if isinstance(allocations, str):
            try:
                allocations = json.loads(allocations)
            except Exception:
                allocations = None
        if isinstance(allocations, list) and allocations:
            for a in allocations:
                if isinstance(a, dict) and a.get("symbol_key") and a.get("value"):
                    out.append({"symbol_key": a["symbol_key"],
                                "value": float(a["value"]),
                                "backtest_id": a.get("backtest_id") or ""})
            return out, cash
        if source.startswith("sim:"):
            acct = await asyncio.get_running_loop().run_in_executor(
                None, _sim_account_sync, source.split(":", 1)[1])
            if not acct:
                return None, 0.0
            val = await _sim_value(acct)
            cash = float(val["cash"])
            for p in val["positions"]:
                if (p.get("qty") or 0) > 0 and p.get("value"):
                    out.append({"symbol_key": p["symbol_key"],
                                "value": float(p["value"]), "backtest_id": ""})
            return out, cash
        r = await lab.cap_portfolio_positions()
        for p in r.get("positions") or []:
            if (p.get("qty") or 0) > 0 and p.get("market_value"):
                out.append({"symbol_key": p["symbol_key"],
                            "value": float(p["market_value"]), "backtest_id": ""})
        return out, cash

    @capability(
        "markets.project.portfolio", http_method="POST",
        http_path="/markets/project/portfolio", http_tags=["markets"],
        memory="off", silent=True,
        description="Project a whole portfolio's value into the future: per-asset "
                    "Monte-Carlo (historical drift/vol, or a linked strategy's "
                    "backtested performance via strategy_map) summed into "
                    "portfolio-level bands, nominal + inflation-adjusted real, "
                    "with optional annual cost drag. Input: source ('portfolio' = "
                    "the real ledger | 'sim:<account_id>') OR allocations "
                    "(list [{symbol_key, value, backtest_id?}]), horizon_days "
                    "(int=365), strategy_map (object {symbol_key: backtest_id} — "
                    "use that strategy's performance for that asset), "
                    "inflation_pct (float — override), annual_costs_pct (float=0). "
                    "Output: {value_now, cash, per_asset:[{symbol_key,value,mode,"
                    "mu_annual_pct,sigma_annual_pct}], inflation, t_days, "
                    "nominal:{p10..p90}, real:{p10..p90}}.",
    )
    async def cap_project_portfolio(source: str = "portfolio", allocations=None,
                                    horizon_days: int = 365, strategy_map=None,
                                    inflation_pct: float = None,
                                    annual_costs_pct: float = 0.0,
                                    trace_id=None) -> dict:
        md = _md()
        holdings, cash = await _resolve_holdings(source or "portfolio", allocations)
        if holdings is None:
            return {"error": f"source '{source}' not found"}
        if not holdings and cash <= 0:
            return {"error": "nothing to project — no positions found"}
        if isinstance(strategy_map, str):
            try:
                strategy_map = json.loads(strategy_map)
            except Exception:
                strategy_map = {}
        strategy_map = strategy_map or {}
        days = max(30, min(3650, int(horizon_days)))
        infl, infl_src = (float(inflation_pct), "override") \
            if inflation_pct is not None else await _inflation_pct()
        drag = float(annual_costs_pct or 0) / 100.0
        loop = asyncio.get_running_loop()
        total_paths = None
        t_days = None
        per_asset = []
        for i, h in enumerate(holdings[:20]):
            prov, sym = (h["symbol_key"].split(":", 1) + [""])[:2] \
                if ":" in h["symbol_key"] else ("binance", h["symbol_key"])
            ds = md._dataset_id(prov, sym, "1d")
            bt_id = h.get("backtest_id") or strategy_map.get(h["symbol_key"]) or ""
            mode, ms = "history", await _hist_mu_sigma(ds)
            if bt_id:
                bms = await _backtest_mu_sigma(bt_id)
                if bms:
                    mode, ms = f"strategy:{bms['name']}", bms
            if not ms:
                per_asset.append({"symbol_key": h["symbol_key"],
                                  "value": h["value"], "error": "no history"})
                continue
            mu = ms["mu"] - drag
            td, _b, paths = await loop.run_in_executor(
                None, _mc_project, h["value"], mu, ms["sigma"], days, 400, 100 + i)
            t_days = td
            total_paths = paths if total_paths is None else total_paths + paths
            per_asset.append({"symbol_key": h["symbol_key"], "value": round(h["value"], 2),
                              "mode": mode,
                              "mu_annual_pct": round((math.exp(mu) - 1) * 100, 2),
                              "sigma_annual_pct": round(ms["sigma"] * 100, 2)})
        if total_paths is None:
            return {"error": "no projectable holdings"}
        if cash > 0:
            total_paths = total_paths + cash
        bands = {f"p{q}": [round(float(x), 2) for x in
                           np.percentile(total_paths, q, axis=0)]
                 for q in (10, 25, 50, 75, 90)}
        value_now = sum(h["value"] for h in holdings) + cash
        return {"source": source, "value_now": round(value_now, 2),
                "cash": round(cash, 2), "per_asset": per_asset,
                "inflation": {"pct": round(infl, 2), "source": infl_src},
                "horizon_days": days, "t_days": t_days,
                "nominal": bands, "real": _real_bands(bands, t_days, infl)}

    @capability(
        "markets.portfolio.optimize", http_method="POST",
        http_path="/markets/portfolio/optimize", http_tags=["markets"], memory="on",
        description="Optimise portfolio weights (Monte-Carlo efficient frontier — "
                    "correlations from aligned daily returns): finds max-Sharpe / "
                    "max-return / min-vol weights over candidate assets, compares "
                    "to current holdings, prices the rebalance (turnover × fees) "
                    "and can EXECUTE it on paper (apply='sim:<account_id>' places "
                    "the sim orders). strategy_map swaps an asset's expected "
                    "return for a strategy's backtested CAGR. Input: candidates "
                    "(list of 'provider:symbol' | 'watchlist'), source "
                    "('portfolio'|'sim:<id>'|'' = value only), value (float=10000), "
                    "objective (sharpe|return|min_vol), max_weight (float=0.4), "
                    "samples (int=4000), fee_bps (float=10), lookback_days "
                    "(int=730), strategy_map (object), apply (''|'sim:<id>'). "
                    "Output: {best:{weights,ret_pct,vol_pct,sharpe}, current, "
                    "frontier:[{ret,vol,sharpe}], trades:[…], est_fee_pct, applied}.",
    )
    async def cap_portfolio_optimize(candidates=None, source: str = "",
                                     value: float = 10_000.0,
                                     objective: str = "sharpe",
                                     max_weight: float = 0.4, samples: int = 4000,
                                     fee_bps: float = 10.0,
                                     lookback_days: int = 730, strategy_map=None,
                                     apply: str = "", trace_id=None) -> dict:
        md, mc = _md(), _mc()
        loop = asyncio.get_running_loop()
        if isinstance(candidates, str) and candidates != "watchlist":
            candidates = [x.strip() for x in candidates.split(",") if x.strip()]
        keys: List[str] = []
        if candidates == "watchlist" or not candidates:
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            keys = [r["id"] for r in rows if r.get("exchange") not in ("macro", "custom")]
        else:
            keys = list(candidates)
        holdings, cash = await _resolve_holdings(source, None) if source else ([], 0.0)
        if holdings:
            keys = list(dict.fromkeys([h["symbol_key"] for h in holdings] + keys))
        keys = keys[:12]
        if len(keys) < 2:
            return {"error": "need ≥2 candidate assets with history"}
        if isinstance(strategy_map, str):
            try:
                strategy_map = json.loads(strategy_map)
            except Exception:
                strategy_map = {}
        strategy_map = strategy_map or {}

        stats = {}
        for k in keys:
            prov, sym = (k.split(":", 1) + [""])[:2] if ":" in k else ("binance", k)
            ms = await _hist_mu_sigma(md._dataset_id(prov, sym, "1d"),
                                      max(200, int(lookback_days)))
            if ms:
                stats[k] = ms
        keys = [k for k in keys if k in stats]
        if len(keys) < 2:
            return {"error": "need ≥2 assets with ≥60 stored daily bars — fetch history first"}
        # aligned daily return matrix
        common = None
        for k in keys:
            days_set = set(stats[k]["day_rets"].keys())
            common = days_set if common is None else (common & days_set)
        common = sorted(common or [])
        if len(common) < 120:
            return {"error": f"only {len(common)} overlapping trading days — "
                             "assets need more shared history"}
        R = np.array([[stats[k]["day_rets"][d] for d in common] for k in keys])
        ppy = max(50.0, len(common) / max(0.2, (common[-1] - common[0]) / 365.0))
        mu = R.mean(axis=1) * ppy
        cov = np.cov(R) * ppy
        for i, k in enumerate(keys):                # strategy CAGR overrides drift
            bt_id = strategy_map.get(k)
            if bt_id:
                bms = await _backtest_mu_sigma(bt_id)
                if bms:
                    mu[i] = bms["mu"]

        def _search():
            rng = np.random.default_rng(11)
            W = rng.dirichlet(np.ones(len(keys)), size=max(500, min(20_000, int(samples))))
            mw = max(1.0 / len(keys) + 0.01, float(max_weight))
            ok = (W.max(axis=1) <= mw)
            W = W[ok] if ok.any() else W
            rets = W @ mu
            vols = np.sqrt(np.maximum(1e-12, np.einsum("ij,jk,ik->i", W, cov, W)))
            sharpes = rets / vols
            obj = {"return": rets, "min_vol": -vols}.get(objective, sharpes)
            bi = int(np.argmax(obj))
            idx = np.linspace(0, len(W) - 1, min(250, len(W))).astype(int)
            frontier = [{"ret": round(float(rets[i]) * 100, 2),
                         "vol": round(float(vols[i]) * 100, 2),
                         "sharpe": round(float(sharpes[i]), 3)} for i in idx]
            return W[bi], float(rets[bi]), float(vols[bi]), float(sharpes[bi]), frontier
        w_best, ret_b, vol_b, sh_b, frontier = await loop.run_in_executor(None, _search)

        total = sum(h["value"] for h in holdings) + cash if holdings or cash \
            else max(100.0, float(value))
        cur_w = np.zeros(len(keys))
        for h in holdings or []:
            if h["symbol_key"] in keys and total > 0:
                cur_w[keys.index(h["symbol_key"])] = h["value"] / total
        cur_ret = float(cur_w @ mu)
        cur_vol = float(math.sqrt(max(1e-12, cur_w @ cov @ cur_w)))
        turnover = float(np.abs(w_best - cur_w).sum()) / 2.0
        est_fee_pct = round(turnover * 2 * float(fee_bps) / 100.0, 3)
        trades = []
        for i, k in enumerate(keys):
            dv = (w_best[i] - cur_w[i]) * total
            if abs(dv) > total * 0.01:
                trades.append({"symbol_key": k,
                               "action": "buy" if dv > 0 else "sell",
                               "value": round(abs(dv), 2),
                               "weight_from": round(float(cur_w[i]) * 100, 1),
                               "weight_to": round(float(w_best[i]) * 100, 1)})
        trades.sort(key=lambda t2: (t2["action"] != "sell", -t2["value"]))
        applied = []
        if apply.startswith("sim:"):
            aid = apply.split(":", 1)[1]
            for tr in trades:                        # sells free the cash first
                r2 = await cap_sim_order(account_id=aid,
                                         symbol_key=tr["symbol_key"],
                                         side=tr["action"], notional=tr["value"],
                                         source="optimizer",
                                         note=f"rebalance {tr['weight_from']}→{tr['weight_to']}%")
                applied.append({**tr, "ok": bool(r2.get("ok")),
                                "error": r2.get("error")})
        await emit_event({"type": "markets.portfolio", "stage": "optimized",
                          "objective": objective, "sharpe": round(sh_b, 3),
                          "trades": len(trades), "applied": bool(applied)})
        return {"ok": True, "assets": keys, "objective": objective,
                "best": {"weights": {k: round(float(w_best[i]) * 100, 1)
                                     for i, k in enumerate(keys)},
                         "ret_pct": round(ret_b * 100, 2),
                         "vol_pct": round(vol_b * 100, 2),
                         "sharpe": round(sh_b, 3)},
                "current": {"weights": {k: round(float(cur_w[i]) * 100, 1)
                                        for i, k in enumerate(keys)},
                            "ret_pct": round(cur_ret * 100, 2),
                            "vol_pct": round(cur_vol * 100, 2),
                            "value": round(total, 2)},
                "frontier": frontier, "trades": trades,
                "est_fee_pct": est_fee_pct,
                "overlap_days": len(common),
                "applied": applied or None}

    @capability(
        "markets.rotation.scan", http_method="POST",
        http_path="/markets/rotation/scan", http_tags=["markets"],
        memory="off", silent=True,
        description="Asset-rotation scanner — find the optimal path OUT of one "
                    "asset INTO another (e.g. BTC→ETH): every candidate is scored "
                    "by blended momentum (1w/1m/3m z-scores), trend regime, live "
                    "signals of accepted strategies monitoring it, and optional ML "
                    "model P(up); held assets scoring well below the leaders get "
                    "switch suggestions with the fee cost of the hop priced in. "
                    "Input: assets (list | 'watchlist'), source (''|'portfolio'|"
                    "'sim:<id>' — what you hold), ml_ids (list — trained models to "
                    "consult), use_strategies (bool=True), margin (float=0.3 — "
                    "min score gap), fee_bps (float=10), tf (str=1d). Output: "
                    "{ranking:[{key,score,momentum_z,trend,strat_signal,ml}], "
                    "switches:[{from,to,edge,est_fee_pct,ratio_spark}], asof}.",
    )
    async def cap_rotation_scan(assets=None, source: str = "", ml_ids=None,
                                use_strategies: bool = True, margin: float = 0.3,
                                fee_bps: float = 10.0, tf: str = "1d",
                                trace_id=None) -> dict:
        md, ma, mc, lab = _md(), _ma(), _mc(), _mlab()
        loop = asyncio.get_running_loop()
        if isinstance(assets, str) and assets != "watchlist":
            assets = [x.strip() for x in assets.split(",") if x.strip()]
        if assets == "watchlist" or not assets:
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            assets = [r["id"] for r in rows
                      if r.get("exchange") not in ("macro", "custom")]
        assets = list(dict.fromkeys(assets))[:20]
        if isinstance(ml_ids, str):
            ml_ids = [x.strip() for x in ml_ids.split(",") if x.strip()]
        # accepted strategies per dataset (live signals)
        strat_by_ds = {}
        if use_strategies:
            try:
                mon = await lab.cap_monitor_status()
                for m in mon.get("monitors") or []:
                    if m.get("dataset_id"):
                        strat_by_ds.setdefault(m["dataset_id"], []).append(m["id"])
            except Exception:
                pass
        rows_out = []
        closes = {}
        for k in assets:
            prov, sym = (k.split(":", 1) + [""])[:2] if ":" in k else ("binance", k)
            ds = md._dataset_id(prov, sym, tf or "1d")
            bars = await md.get_bars(ds, 320)
            if len(bars) < 40:
                continue
            arr = ma.bars_to_arrays(bars)
            c = arr["c"]
            closes[k] = c
            n2 = len(c)

            def chg(kk):
                return (c[-1] / c[-1 - kk] - 1.0) * 100 if n2 > kk and c[-1 - kk] > 0 else None
            r1w, r1m, r3m = chg(5), chg(21), chg(63)
            m2 = min(90, n2)
            b2, _a2, _r2 = ols_fit(np.arange(m2, dtype=np.float64) / m2,
                                   np.log(np.maximum(1e-12, c[-m2:])))
            yrs2 = max(1e-9, (arr["t"][-1] - arr["t"][-m2]) / (365.0 * 86400.0))
            slope_yr = (math.exp(b2 / yrs2) - 1.0) * 100
            trend = 1 if slope_yr >= 12 else (-1 if slope_yr <= -12 else 0)
            strat_sig = None
            for sid in (strat_by_ds.get(ds) or [])[:2]:
                try:
                    srow = await loop.run_in_executor(None, lab._strategy_full_sync, sid)
                    if srow:
                        e, x, se, sx = await lab._spec_signals(arr, srow["spec"])
                        strat_sig = (1 if bool(e[-1]) else 0) - (1 if bool(x[-1]) else 0)
                        if se is not None and bool(se[-1]):
                            strat_sig = -1
                except Exception:
                    continue
            ml_p = None
            for mid in (ml_ids or [])[:2]:
                try:
                    sig = await lab._ml_signal_series(str(mid), arr)
                    lastv = next((float(v) for v in sig[::-1]
                                  if not (isinstance(v, float) and math.isnan(v))), None)
                    if lastv is not None:
                        ml_p = lastv
                except Exception:
                    continue
            rows_out.append({"key": k, "r1w": r1w, "r1m": r1m, "r3m": r3m,
                             "trend": trend, "trend_slope_pct_year": round(slope_yr, 1),
                             "strat_signal": strat_sig, "ml": ml_p})
        if len(rows_out) < 2:
            return {"error": "need ≥2 assets with stored bars"}
        # momentum z-scores across the cohort → composite
        for f, w in (("r1w", 0.2), ("r1m", 0.4), ("r3m", 0.4)):
            vals = [r.get(f) for r in rows_out if r.get(f) is not None]
            mmean = float(np.mean(vals)) if vals else 0.0
            msd = float(np.std(vals)) if vals else 1.0
            for r in rows_out:
                z = ((r.get(f) or mmean) - mmean) / (msd or 1.0)
                r["momentum_z"] = r.get("momentum_z", 0.0) + w * z
        for r in rows_out:
            score, wsum = 0.45 * r["momentum_z"], 0.45
            score += 0.2 * r["trend"]; wsum += 0.2
            if r.get("strat_signal") is not None:
                score += 0.15 * r["strat_signal"]; wsum += 0.15
            if r.get("ml") is not None:
                score += 0.2 * (r["ml"] - 0.5) * 2; wsum += 0.2
            r["score"] = round(score / wsum, 3)
            r["momentum_z"] = round(r["momentum_z"], 3)
        rows_out.sort(key=lambda r: -r["score"])
        # switch suggestions out of held laggards into the leaders
        held = []
        if source:
            holdings, _cash = await _resolve_holdings(source, None)
            held = [h["symbol_key"] for h in holdings or []]
        best = rows_out[0]
        switches = []
        for r in rows_out:
            if r["key"] not in held or r["key"] == best["key"]:
                continue
            edge = best["score"] - r["score"]
            fee_cost = 2 * float(fee_bps) / 100.0
            if edge >= float(margin):
                spark = []
                ca, cb = closes.get(r["key"]), closes.get(best["key"])
                if ca is not None and cb is not None:
                    m3 = min(90, len(ca), len(cb))
                    ratio = ca[-m3:] / np.maximum(1e-12, cb[-m3:])
                    step = max(1, m3 // 45)
                    spark = [round(float(x), 6) for x in ratio[::step]]
                switches.append({"from": r["key"], "to": best["key"],
                                 "edge": round(edge, 3),
                                 "est_fee_pct": round(fee_cost, 3),
                                 "ratio_spark": spark,
                                 "reason": f"score {r['score']} → {best['score']} "
                                           f"(momentum z {r['momentum_z']} vs "
                                           f"{best['momentum_z']})"})
        return {"ok": True, "ranking": rows_out, "switches": switches,
                "held": held, "asof": now_iso()}

    # ── Baseline estate + market overview ────────────────────────────────────

    @capability(
        "markets.baseline.list", http_method="GET", http_path="/markets/baseline",
        http_tags=["markets"], memory="off", silent=True,
        description="The curated baseline estate (indices, all SPDR sectors, rates, "
                    "FX, commodities, BTC/ETH) with tracked/bar status. "
                    "Output: {assets:[{provider,symbol,name,group,tracked,bars}]}.",
    )
    async def cap_baseline_list(trace_id=None) -> dict:
        mc, md = _mc(), _md()
        loop = asyncio.get_running_loop()
        tracked = {}
        if mc:
            try:
                rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
                tracked = {r["id"]: r for r in rows}
            except Exception:
                pass
        out = []
        for a in BASELINE_ASSETS:
            key = f"{a['provider']}:{a['symbol']}"
            ds = md._dataset_id(a["provider"], a["symbol"], "1d") if md else ""
            last_ms = await loop.run_in_executor(None, md._last_bar_ms_sync, ds) \
                if md else None
            out.append({**a, "key": key, "tracked": key in tracked,
                        "last_bar": md._ms_to_iso(last_ms) if (md and last_ms) else None})
        return {"assets": out, "count": len(out)}

    @capability(
        "markets.baseline.ensure", http_method="POST",
        http_path="/markets/baseline/ensure", http_tags=["markets"], memory="on",
        description="Track the whole baseline estate: adds any missing baseline "
                    "asset to the watchlist (1d bars, auto-update) and starts "
                    "backfills for assets with no stored history — one call makes "
                    "the market/sector infographics live. Input: update_interval_min "
                    "(int=240), timeframes (list=['1d']), groups (list — subset of "
                    "Index|Sectors|Rates & FX|Commodities|Crypto; default all). "
                    "Output: {ok, added:[…], backfilling:[…], already:[…]}.",
    )
    async def cap_baseline_ensure(update_interval_min: int = 240, timeframes=None,
                                  groups=None, trace_id=None) -> dict:
        mc, md = _mc(), _md()
        if not (mc and md):
            return {"error": "markets modules unavailable"}
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        if isinstance(timeframes, str):
            timeframes = [t.strip() for t in timeframes.split(",") if t.strip()]
        tfs = [t for t in (timeframes or ["1d"]) if t]
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
        have = {r["id"] for r in rows}
        added, backfilling, already = [], [], []
        for a in BASELINE_ASSETS:
            if groups and a["group"] not in groups:
                continue
            key = f"{a['provider']}:{a['symbol']}"
            if key in have:
                already.append(key)
            else:
                r = await mc.cap_markets_watchlist_add(
                    exchange=a["provider"], symbol=a["symbol"], timeframes=tfs,
                    auto_update=True, update_interval_min=max(5, int(update_interval_min)),
                    backfill=False)
                if r.get("error"):
                    continue
                added.append(key)
            ds = md._dataset_id(a["provider"], a["symbol"], "1d")
            last_ms = await loop.run_in_executor(None, md._last_bar_ms_sync, ds)
            if last_ms is None:
                fr = await mc.cap_markets_fetch(exchange=a["provider"],
                                                symbol=a["symbol"],
                                                timeframes=tfs, full=True)
                if fr.get("ok"):
                    backfilling.append(key)
        await emit_event({"type": "markets.baseline", "stage": "ensured",
                          "added": len(added), "backfilling": len(backfilling)})
        return {"ok": True, "added": added, "backfilling": backfilling,
                "already": already}

    _OVERVIEW_CACHE = {"ts": 0.0, "data": None}

    def _pct(cur: float, prev: float) -> Optional[float]:
        if prev is None or cur is None or prev <= 0:
            return None
        return round((cur / prev - 1.0) * 100.0, 3)

    @capability(
        "markets.overview", http_method="GET", http_path="/markets/overview",
        http_tags=["markets"], memory="off", silent=True,
        description="Whole-market / per-sector / per-asset overview computed from "
                    "stored 1d bars for every watched asset (incl. the baseline "
                    "estate): last price, changes over 1d/1w/1m/3m/6m/ytd/1y, 30d "
                    "annualised vol, RSI, 52-week range position, trend label + "
                    "annualised slope, sparkline. Grouped with per-group breadth "
                    "(advancers %) and median moves — feeds the live market "
                    "infographics. Input: refresh (bool=False — bypass 60s cache), "
                    "spark (int=90 — sparkline points, 0=off). "
                    "Output: {groups:[{name,assets:[…],breadth_1d,breadth_1m,"
                    "median_1d,median_1m}], asof, count}.",
    )
    async def cap_markets_overview(refresh: bool = False, spark: int = 90,
                                   trace_id=None) -> dict:
        if not refresh and _OVERVIEW_CACHE["data"] and \
                time.time() - _OVERVIEW_CACHE["ts"] < 60:
            return _OVERVIEW_CACHE["data"]
        mc, md, ma = _mc(), _md(), _ma()
        if not (mc and md and ma):
            return {"error": "markets modules unavailable"}
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
        seen, assets = set(), []
        for r in rows:
            key = r["id"]
            if key in seen:
                continue
            # metrics (macro series, positioning/OSINT series) are NOT market
            # assets — they get their own Metrics rail in Pulse, never the map
            if r.get("exchange") in ("macro", "dyn"):
                continue
            seen.add(key)
            meta = _BASELINE_BY_KEY.get(key)
            prov, sym = r["exchange"], r["symbol"]
            group = meta["group"] if meta else \
                ("Crypto" if prov in ("binance", "coinbase", "kraken", "bybit")
                 else "Watchlist")
            ds = md._dataset_id(prov, sym, "1d")
            bars = await md.get_bars(ds, 420)
            if len(bars) < 10:
                assets.append({"key": key, "symbol": sym, "provider": prov,
                               "name": (meta or {}).get("name") or sym,
                               "group": group, "sparse": True, "bars": len(bars)})
                continue
            arr = ma.bars_to_arrays(bars)
            c, t = arr["c"], arr["t"]
            last = float(c[-1])
            n = len(c)

            def back(k):
                return float(c[n - 1 - k]) if n - 1 - k >= 0 else None
            year = datetime.utcfromtimestamp(int(t[-1])).year
            ytd_px = None
            for i in range(n):
                if datetime.utcfromtimestamp(int(t[i])).year == year:
                    ytd_px = float(c[i - 1]) if i > 0 else float(c[0])
                    break
            rets = np.diff(np.log(np.maximum(1e-12, c[-31:])))
            vol30 = round(float(np.std(rets)) * math.sqrt(252) * 100, 2) \
                if len(rets) > 5 else None
            try:
                rsi = ma._rsi(c, 14)
                rsi_v = round(float(rsi[-1]), 1) if not math.isnan(rsi[-1]) else None
            except Exception:
                rsi_v = None
            w52 = c[-252:] if n >= 252 else c
            hi52, lo52 = float(np.max(w52)), float(np.min(w52))
            rng_pos = round((last - lo52) / (hi52 - lo52) * 100, 1) \
                if hi52 > lo52 else None
            # quick trend: OLS on log close over last 90 bars, annualised
            m = min(90, n)
            tb, ta_, _r2 = ols_fit(
                (t[-m:] - t[-m]) / max(1.0, (t[-1] - t[-m])) if t[-1] > t[-m]
                else np.arange(m, dtype=np.float64),
                np.log(np.maximum(1e-12, c[-m:])))
            yrs = max(1e-9, (t[-1] - t[-m]) / (365.0 * 86400.0))
            slope_yr = (math.exp(tb / yrs) - 1.0) * 100.0 if yrs > 0 else 0.0
            trend = "bull" if slope_yr >= 12 else ("bear" if slope_yr <= -12 else "flat")
            a = {
                "key": key, "symbol": sym, "provider": prov,
                "name": (meta or {}).get("name") or sym, "group": group,
                "last": round(last, 6), "bars": n,
                "chg_1d": _pct(last, back(1)), "chg_1w": _pct(last, back(5)),
                "chg_1m": _pct(last, back(21)), "chg_3m": _pct(last, back(63)),
                "chg_6m": _pct(last, back(126)), "chg_1y": _pct(last, back(252)),
                "chg_ytd": _pct(last, ytd_px),
                "vol_30d_pct": vol30, "rsi": rsi_v,
                "range_52w_pct": rng_pos,
                "hi_52w": round(hi52, 6), "lo_52w": round(lo52, 6),
                "trend": trend, "trend_slope_pct_year": round(slope_yr, 2),
                "asof": int(t[-1]),
            }
            k = max(0, min(240, int(spark)))
            if k:
                cs = c[-k * 2:]
                if len(cs) > k:
                    step = len(cs) / k
                    cs = np.asarray([cs[int(i * step)] for i in range(k)] + [cs[-1]])
                a["spark"] = [round(float(x), 6) for x in cs.tolist()]
            assets.append(a)

        groups = []
        for gname in GROUP_ORDER:
            ga = [a for a in assets if a.get("group") == gname]
            if not ga:
                continue
            ch1 = [a["chg_1d"] for a in ga if a.get("chg_1d") is not None]
            ch1m = [a["chg_1m"] for a in ga if a.get("chg_1m") is not None]
            groups.append({
                "name": gname, "assets": ga, "count": len(ga),
                "breadth_1d": round(sum(1 for v in ch1 if v > 0) / len(ch1) * 100, 1)
                    if ch1 else None,
                "breadth_1m": round(sum(1 for v in ch1m if v > 0) / len(ch1m) * 100, 1)
                    if ch1m else None,
                "median_1d": round(float(np.median(ch1)), 3) if ch1 else None,
                "median_1m": round(float(np.median(ch1m)), 3) if ch1m else None,
            })
        out = {"groups": groups, "count": len(assets), "asof": now_iso()}
        _OVERVIEW_CACHE.update({"ts": time.time(), "data": out})
        return out

    # ── Key dates / events ───────────────────────────────────────────────────

    def _http_json_sync(url: str, params: dict = None) -> dict:
        import urllib.parse
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (VeraMarkets)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _http_text_sync(url: str) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (VeraMarkets)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def _yahoo_events_sync(symbol: str) -> List[dict]:
        """IPO/first-trade + dividends + splits from the yahoo chart API."""
        out: List[dict] = []
        try:
            j = _http_json_sync(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                {"interval": "1mo", "range": "max", "events": "div,splits"})
            res = (((j or {}).get("chart") or {}).get("result") or [{}])[0]
            meta = res.get("meta") or {}
            ftd = meta.get("firstTradeDate")
            if ftd:
                out.append({"kind": "ipo", "t": int(ftd),
                            "text": f"{symbol} first trade"})
            evs = res.get("events") or {}
            divs = sorted((evs.get("dividends") or {}).values(),
                          key=lambda d: d.get("date") or 0)
            for d in divs[-12:]:
                if d.get("date"):
                    out.append({"kind": "dividend", "t": int(d["date"]),
                                "text": f"Dividend {d.get('amount')}"})
            for s in (evs.get("splits") or {}).values():
                if s.get("date"):
                    out.append({"kind": "split", "t": int(s["date"]),
                                "text": f"Split {s.get('numerator')}:{s.get('denominator')}"})
        except Exception as e:
            log.debug("yahoo events %s: %s", symbol, e)
        return out

    @capability(
        "markets.events.detect", http_method="GET", http_path="/markets/events/detect",
        http_tags=["markets"], memory="off", silent=True,
        description="Detect key dates for an asset: crypto halvings & network "
                    "upgrades, market-wide shocks (COVID, FTX, elections, ETF "
                    "approvals), and for yahoo assets the IPO/first-trade date, "
                    "dividends and splits. Input: symbol_key (str! — "
                    "'provider:symbol'). Output: {events:[{kind,t,text,projected}]}.",
    )
    async def cap_events_detect(symbol_key: str = "", trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        prov, sym = (symbol_key.split(":", 1) + [""])[:2] if ":" in symbol_key \
            else ("binance", symbol_key)
        base = sym.split("/")[0].split("-")[0].lower()
        events: List[dict] = []
        for e in EVENT_LIBRARY:
            m = e.get("match") or []
            if "*" in m or base in m:
                item = {k: e[k] for k in ("kind", "t", "text")}
                if e.get("projected"):
                    item["projected"] = True
                events.append(item)
        if prov == "yahoo":
            found = await asyncio.get_running_loop().run_in_executor(
                None, _yahoo_events_sync, sym)
            events.extend(found)
        events.sort(key=lambda e: e["t"])
        return {"symbol_key": symbol_key, "events": events, "count": len(events)}

    @capability(
        "markets.events.apply", http_method="POST", http_path="/markets/events/apply",
        http_tags=["markets"], memory="on",
        description="Write an asset's detected key dates onto its chart as vline "
                    "annotations (author 'events', colour-coded by kind, deduped). "
                    "Input: symbol_key (str!), kinds (list — filter e.g. "
                    "['halving','macro','ipo','dividend','split','upgrade']; default "
                    "all except dividends). Output: {ok, applied, skipped}.",
    )
    async def cap_events_apply(symbol_key: str = "", kinds=None, trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        ma = _ma()
        if not ma:
            return {"error": "markets analysis module unavailable"}
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",") if k.strip()]
        det = await cap_events_detect(symbol_key=symbol_key)
        if det.get("error"):
            return det
        want = set(kinds) if kinds else {"halving", "upgrade", "macro", "ipo", "split"}
        existing = await asyncio.get_running_loop().run_in_executor(
            None, ma._ann_rows_sync, symbol_key, "")
        have = set()
        for a in existing:
            if a.get("author") != "events":
                continue
            for p in a.get("points") or []:
                if p.get("t") is not None:
                    have.add((str((a.get("meta") or {}).get("event") or ""), int(p["t"])))
        applied, skipped = 0, 0
        for e in det.get("events") or []:
            if e["kind"] not in want:
                continue
            sig = (e["kind"], int(e["t"]))
            if sig in have:
                skipped += 1
                continue
            r = await ma.cap_annotate_add(
                symbol_key=symbol_key, kind="vline", points=[{"t": int(e["t"])}],
                text=e["text"], color=EVENT_COLORS.get(e["kind"], "#8a93a6"),
                author="events", meta={"event": e["kind"],
                                       "projected": bool(e.get("projected"))})
            if r.get("ok"):
                applied += 1
        await emit_event({"type": "markets.events", "stage": "applied",
                          "symbol_key": symbol_key, "applied": applied})
        return {"ok": True, "symbol_key": symbol_key,
                "applied": applied, "skipped": skipped}

    # ── Macro / on-chain layer datasets ──────────────────────────────────────

    def _fred_rows_sync(series: str) -> List[list]:
        txt = _http_text_sync(
            f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
        rows: List[list] = []
        for line in txt.splitlines()[1:]:
            parts = line.strip().split(",")
            if len(parts) < 2 or parts[1] in (".", ""):
                continue
            try:
                ts = int(datetime.fromisoformat(parts[0])
                         .replace(tzinfo=timezone.utc).timestamp() * 1000)
                v = float(parts[1])
            except Exception:
                continue
            rows.append([ts, v, v, v, v, 0.0])
        return rows

    def _chain_rows_sync(chart: str) -> List[list]:
        j = _http_json_sync(f"https://api.blockchain.info/charts/{chart}",
                            {"timespan": "all", "format": "json", "sampled": "true"})
        rows: List[list] = []
        for p in (j or {}).get("values") or []:
            try:
                rows.append([int(p["x"]) * 1000, float(p["y"]), float(p["y"]),
                             float(p["y"]), float(p["y"]), 0.0])
            except Exception:
                continue
        return rows

    def _macro_rows_sync(mid: str) -> List[list]:
        m = _MACRO_BY_ID.get(mid)
        if not m:
            raise ValueError(f"unknown macro series '{mid}'")
        if m.get("derive") == "yoy":
            base = _fred_rows_sync(m["derived_from"])
            out = []
            for i, row in enumerate(base):
                target = row[0] - 365 * 86400_000
                prev = None
                for j in range(i - 1, -1, -1):
                    if abs(base[j][0] - target) <= 45 * 86400_000:
                        prev = base[j][4]
                        break
                    if base[j][0] < target - 45 * 86400_000:
                        break
                if prev and prev > 0:
                    v = (row[4] / prev - 1.0) * 100.0
                    out.append([row[0], v, v, v, v, 0.0])
            return out
        if m["source"] == "fred":
            return _fred_rows_sync(mid.split(":", 1)[1])
        if m["source"] == "chain":
            return _chain_rows_sync(mid.split(":", 1)[1])
        raise ValueError(f"unknown macro source for '{mid}'")

    async def _macro_ingest_timeframe(job: dict, symbol: str, tf: str,
                                      full: bool) -> int:
        """PROVIDER_INGESTORS entry — lets macro series ride the shared job
        runner + watchlist auto-update scheduler (provider id 'macro')."""
        md = _md()
        ds = md._dataset_id("macro", symbol, "1d")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, md._ensure_dataset_sync, ds, "macro",
                                   "1d", ["macro"])
        last_ms = await loop.run_in_executor(None, md._last_bar_ms_sync, ds)
        try:
            rows = await loop.run_in_executor(None, _macro_rows_sync, symbol)
        except Exception as e:
            job["errors"][tf] = str(e)[:300]
            return 0
        floor = -1 if (full or last_ms is None) else last_ms
        n = await md._write_bars(ds, "macro", symbol, "1d", rows, floor_ms=floor)
        job["fetched"][tf] = n
        job["stage"] = f"{symbol}: {n} points"
        total = await loop.run_in_executor(None, md._reconcile_count_sync, ds)
        job["totals"][tf] = total
        await emit_event({"type": "markets.fetch", "job_id": job.get("job_id"),
                          "stage": "progress", "exchange": "macro",
                          "symbol": symbol, "timeframe": "1d", "fetched": n})
        return n

    def _register_macro_provider():
        md = _md()
        if md is not None and hasattr(md, "PROVIDER_INGESTORS"):
            md.PROVIDER_INGESTORS["macro"] = _macro_ingest_timeframe
    _register_macro_provider()

    @capability(
        "markets.macro.catalog", http_method="GET", http_path="/markets/macro/catalog",
        http_tags=["markets"], memory="off", silent=True,
        description="Catalog of layerable macro / on-chain series (FRED rates & "
                    "inflation & money supply, BTC hash rate / transactions / "
                    "addresses / supply / miner revenue / market cap) with fetch "
                    "state. Each fetched series is an ordinary dataset "
                    "'mkt.macro.<slug>.1d' readable via markets.bars and layerable "
                    "on any chart. Output: {series:[{id,name,unit,group,source,"
                    "dataset_id,fetched,last}]}.",
    )
    async def cap_macro_catalog(trace_id=None) -> dict:
        md = _md()
        loop = asyncio.get_running_loop()
        out = []
        for m in MACRO_CATALOG:
            ds = md._dataset_id("macro", m["id"], "1d") if md else ""
            last_ms = await loop.run_in_executor(None, md._last_bar_ms_sync, ds) \
                if md else None
            out.append({**m, "dataset_id": ds, "fetched": last_ms is not None,
                        "last": md._ms_to_iso(last_ms) if (md and last_ms) else None})
        return {"series": out, "count": len(out)}

    @capability(
        "markets.macro.fetch", http_method="POST", http_path="/markets/macro/fetch",
        http_tags=["markets"], memory="on",
        description="Fetch/refresh macro & on-chain series into layer datasets "
                    "(background jobs; progress via markets.fetch events). Input: "
                    "id (str — one series e.g. 'fred:DGS10') OR ids (list), "
                    "track (bool=True — add to the watchlist so it auto-refreshes "
                    "daily), full (bool=True). Output: {ok, job_ids, datasets}.",
    )
    async def cap_macro_fetch(id: str = "", ids=None, track: bool = True,
                              full: bool = True, trace_id=None) -> dict:
        mc, md = _mc(), _md()
        if not (mc and md):
            return {"error": "markets modules unavailable"}
        _register_macro_provider()
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:
                ids = [x.strip() for x in ids.split(",") if x.strip()]
        want = list(ids or [])
        if id:
            want.append(id)
        want = [w for w in want if w in _MACRO_BY_ID]
        if not want:
            return {"error": "id or ids required",
                    "valid": [m["id"] for m in MACRO_CATALOG]}
        job_ids, datasets = [], []
        for mid in want[:16]:
            job = mc._new_job("macro", mid, ["1d"], bool(full))
            job_ids.append(job["job_id"])
            datasets.append(md._dataset_id("macro", mid, "1d"))
            asyncio.create_task(mc._ingest_job(job["job_id"], "macro", mid,
                                               ["1d"], bool(full)))
            if track:
                try:
                    await mc.cap_markets_watchlist_add(
                        exchange="macro", symbol=mid, timeframes=["1d"],
                        auto_update=True, update_interval_min=720, backfill=False)
                except Exception:
                    pass
        await emit_event({"type": "markets.macro", "stage": "fetching",
                          "series": want, "jobs": job_ids})
        return {"ok": True, "job_ids": job_ids, "datasets": datasets}

    # ── Sim accounts (paper trading for humans, agents & monitors) ───────────

    def _sim_accounts_sync() -> List[dict]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM mkt_sim_accounts ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _sim_account_sync(aid: str) -> Optional[dict]:
        conn = _sqlite_conn()
        try:
            r = conn.execute("SELECT * FROM mkt_sim_accounts WHERE id=?",
                             (aid,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def _sim_orders_sync(aid: str, limit: int = 500) -> List[dict]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM mkt_sim_orders WHERE account_id=? "
                "ORDER BY ts DESC LIMIT ?", (aid, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _sim_positions_from_orders(orders: List[dict]) -> Dict[str, dict]:
        pos: Dict[str, dict] = {}
        for o in sorted(orders, key=lambda x: x.get("ts") or ""):
            k = o["symbol_key"]
            p = pos.setdefault(k, {"symbol_key": k, "qty": 0.0, "cost": 0.0,
                                   "realized": 0.0, "fees": 0.0})
            q, px = float(o["qty"]), float(o["price"])
            p["fees"] += float(o.get("fees") or 0)
            if o["side"] == "buy":
                p["cost"] += q * px
                p["qty"] += q
            else:
                if p["qty"] > 1e-12:
                    avg = p["cost"] / p["qty"]
                    take = min(q, p["qty"])
                    p["realized"] += take * (px - avg)
                    p["cost"] -= take * avg
                    p["qty"] -= take
        for p in pos.values():
            p["avg_cost"] = p["cost"] / p["qty"] if p["qty"] > 1e-12 else 0.0
        return pos

    def _sim_sleeves_from_orders(orders: List[dict]) -> Dict[str, Dict[str, float]]:
        """Per-symbol breakdown of open qty BY SOURCE ('user', 'strategy:<id>',
        'template', 'optimizer', …) — this is what lets several strategies (or
        the same strategy on different timeframes) run their own position
        sleeves on ONE account without selling each other's inventory."""
        out: Dict[str, Dict[str, float]] = {}
        for o in sorted(orders, key=lambda x: x.get("ts") or ""):
            src = str(o.get("source") or "user")
            k = o["symbol_key"]
            by = out.setdefault(k, {})
            q = float(o["qty"])
            by[src] = by.get(src, 0.0) + (q if o["side"] == "buy" else -q)
        for k in list(out):
            out[k] = {s: round(q, 10) for s, q in out[k].items() if q > 1e-10}
            if not out[k]:
                del out[k]
        return out

    def _source_qty_sync(account_id: str, symbol_key: str, source: str) -> float:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT side, qty FROM mkt_sim_orders WHERE account_id=? AND "
                "symbol_key=? AND source=?", (account_id, symbol_key, source)).fetchall()
            q = 0.0
            for r in rows:
                q += float(r[1]) if r[0] == "buy" else -float(r[1])
            return max(0.0, q)
        finally:
            conn.close()

    async def _sim_value(acct: dict) -> dict:
        """Account valuation: cash + Σ position × latest stored price."""
        md = _md()
        orders = await asyncio.get_running_loop().run_in_executor(
            None, _sim_orders_sync, acct["id"], 2000)
        pos = _sim_positions_from_orders(orders)
        sleeves = _sim_sleeves_from_orders(orders)
        out_pos, total = [], float(acct["cash"])
        for k, p in pos.items():
            if p["qty"] <= 1e-12:
                if abs(p["realized"]) > 1e-9:
                    out_pos.append({**p, "last": None, "value": 0.0,
                                    "unrealized": 0.0})
                continue
            prov, sym = (k.split(":", 1) + [""])[:2] if ":" in k else ("binance", k)
            last = None
            if md:
                try:
                    last = await md.latest_price(prov, sym)
                except Exception:
                    last = None
            val = p["qty"] * last if last is not None else p["cost"]
            total += val
            out_pos.append({
                "symbol_key": k, "qty": round(p["qty"], 10),
                "avg_cost": round(p["avg_cost"], 8),
                "last": last, "value": round(val, 2),
                "unrealized": round(val - p["cost"], 2) if last is not None else 0.0,
                "realized": round(p["realized"] - p["fees"], 2),
                "sleeves": sleeves.get(k) or None,
            })
        out_pos.sort(key=lambda p: -(p.get("value") or 0))
        start = float(acct.get("start_cash") or 0) or 1.0
        return {"positions": out_pos, "value": round(total, 2),
                "cash": round(float(acct["cash"]), 2),
                "ret_pct": round((total / start - 1.0) * 100.0, 3)}

    async def _sim_snapshot(aid: str):
        acct = await asyncio.get_running_loop().run_in_executor(
            None, _sim_account_sync, aid)
        if not acct:
            return
        val = await _sim_value(acct)

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_sim_equity (id,account_id,ts,value,cash) "
                    "VALUES (?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], aid, now_iso(), val["value"],
                     val["cash"]))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)

    @capability(
        "markets.sim.create", http_method="POST", http_path="/markets/sim/create",
        http_tags=["markets"], memory="on",
        description="Create a paper-trading sim account (for you, agents or "
                    "strategy monitors). Pass template_id (see markets.sim."
                    "templates) to seed it as a typed portfolio profile — the "
                    "template's holdings are bought at current stored prices. "
                    "Input: name (str!), cash (float=100000 — overridden by the "
                    "template's default unless set), currency (str=USD), fee_bps "
                    "(float=10), template_id (str). Output: {ok, id, seeded, "
                    "skipped}.",
    )
    async def cap_sim_create(name: str = "", cash: float = 100_000.0,
                             currency: str = "USD", fee_bps: float = 10.0,
                             template_id: str = "", trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        tpl = next((t for t in SIM_TEMPLATES if t["id"] == template_id), None) \
            if template_id else None
        if template_id and not tpl:
            return {"error": f"unknown template '{template_id}'",
                    "valid": [t["id"] for t in SIM_TEMPLATES]}
        if tpl and (not cash or float(cash) == 100_000.0):
            cash = float(tpl.get("cash") or 100_000.0)
        await _ensure_tables()
        aid = uuid.uuid4().hex[:10]

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_sim_accounts (id,name,start_cash,cash,currency,"
                    "meta,created_at) VALUES (?,?,?,?,?,?,?)",
                    (aid, name.strip(), float(cash), float(cash), currency or "USD",
                     json.dumps({"fee_bps": float(fee_bps)}), now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.sim", "stage": "created", "id": aid,
                          "name": name.strip(), "cash": float(cash),
                          "template": template_id or None})
        seeded, skipped = [], []
        if tpl:
            for hld in tpl.get("holdings") or []:
                r = await cap_sim_order(
                    account_id=aid, symbol_key=hld["symbol_key"], side="buy",
                    notional=float(cash) * float(hld["weight_pct"]) / 100.0,
                    source="template", note=f"seed {tpl['id']}")
                if r.get("ok"):
                    seeded.append(hld["symbol_key"])
                else:
                    skipped.append({"symbol_key": hld["symbol_key"],
                                    "error": r.get("error")})
        return {"ok": True, "id": aid, "seeded": seeded, "skipped": skipped}

    @capability(
        "markets.sim.list", http_method="GET", http_path="/markets/sim/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List sim accounts with live valuation, P&L and positions — "
                    "the paper-trading leaderboard. Output: {accounts:[{id,name,"
                    "cash,value,ret_pct,positions:[…]}]}.",
    )
    async def cap_sim_list(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _sim_accounts_sync)
        out = []
        for acct in rows:
            val = await _sim_value(acct)
            meta = {}
            try:
                meta = json.loads(acct.get("meta") or "{}")
            except Exception:
                pass
            out.append({"id": acct["id"], "name": acct["name"],
                        "currency": acct.get("currency"),
                        "start_cash": acct.get("start_cash"),
                        "created_at": acct.get("created_at"),
                        "fee_bps": meta.get("fee_bps", 10), **val})
        out.sort(key=lambda a: -(a.get("ret_pct") or 0))
        return {"accounts": out, "count": len(out)}

    @capability(
        "markets.sim.order", http_method="POST", http_path="/markets/sim/order",
        http_tags=["markets"], memory="on",
        schema=enum_schema(side=["buy", "sell"]),
        description="Place a sim market order, filled at the latest stored price "
                    "(or an explicit price). Sizing: qty OR notional (quote amount) "
                    "OR pct (buy = % of cash, sell = % of position). Input: "
                    "account_id (str!), symbol_key (str! — 'provider:symbol'), side "
                    "(buy|sell), qty (float), notional (float), pct (float), price "
                    "(float — override), note (str), source (str='user'). "
                    "Output: {ok, order:{…}, account:{cash,value}}.",
    )
    async def cap_sim_order(account_id: str = "", symbol_key: str = "",
                            side: str = "buy", qty: float = 0.0,
                            notional: float = 0.0, pct: float = 0.0,
                            price: float = 0.0, note: str = "",
                            source: str = "user", trace_id=None) -> dict:
        if not account_id or not symbol_key:
            return {"error": "account_id and symbol_key required"}
        side = side if side in ("buy", "sell") else "buy"
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        acct = await loop.run_in_executor(None, _sim_account_sync, account_id)
        if not acct:
            return {"error": "no such sim account", "id": account_id}
        md = _md()
        prov, sym = (symbol_key.split(":", 1) + [""])[:2] if ":" in symbol_key \
            else ("binance", symbol_key)
        px = float(price) if price and float(price) > 0 else None
        if px is None and md:
            try:
                px = await md.latest_price(prov, sym)
            except Exception:
                px = None
        if px is None or px <= 0:
            return {"error": f"no stored price for {symbol_key} — fetch bars first"}
        meta = {}
        try:
            meta = json.loads(acct.get("meta") or "{}")
        except Exception:
            pass
        fee_rate = float(meta.get("fee_bps", 10)) / 10_000.0
        cash = float(acct["cash"])
        orders = await loop.run_in_executor(None, _sim_orders_sync,
                                            account_id, 2000)
        pos = _sim_positions_from_orders(orders).get(symbol_key,
                                                     {"qty": 0.0})
        q = float(qty or 0)
        if q <= 0 and notional and float(notional) > 0:
            q = float(notional) / px
        if q <= 0 and pct and float(pct) > 0:
            p = max(0.1, min(100.0, float(pct))) / 100.0
            q = (cash * p / (px * (1 + fee_rate))) if side == "buy" \
                else float(pos.get("qty") or 0) * p
        if q <= 1e-12:
            return {"error": "size the order with qty, notional or pct"}
        if side == "buy":
            spend = q * px
            fees = spend * fee_rate
            if spend + fees > cash + 1e-9:
                afford = cash / (px * (1 + fee_rate))
                if afford <= 1e-12:
                    return {"error": f"insufficient cash ({cash:.2f})"}
                q, spend, fees = afford, afford * px, afford * px * fee_rate
            new_cash = cash - spend - fees
        else:
            held = float(pos.get("qty") or 0)
            if held <= 1e-12:
                return {"error": f"no {symbol_key} position to sell"}
            q = min(q, held)
            proceeds = q * px
            fees = proceeds * fee_rate
            new_cash = cash + proceeds - fees
        oid = uuid.uuid4().hex[:10]

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_sim_orders (id,account_id,symbol_key,side,qty,"
                    "price,fees,note,source,ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (oid, account_id, symbol_key, side, q, px, fees,
                     note or "", source or "user", now_iso()))
                conn.execute("UPDATE mkt_sim_accounts SET cash=? WHERE id=?",
                             (new_cash, account_id))
                conn.commit()
            finally:
                conn.close()
        await loop.run_in_executor(None, _ins)
        await _sim_snapshot(account_id)
        order = {"id": oid, "account_id": account_id, "symbol_key": symbol_key,
                 "side": side, "qty": round(q, 10), "price": px,
                 "fees": round(fees, 4), "source": source or "user"}
        await emit_event({"type": "markets.sim", "stage": "order", **order})
        acct2 = await loop.run_in_executor(None, _sim_account_sync, account_id)
        val = await _sim_value(acct2)
        return {"ok": True, "order": order,
                "account": {"cash": val["cash"], "value": val["value"],
                            "ret_pct": val["ret_pct"]}}

    async def sim_execute_signal(account_id: str, symbol_key: str, side: str,
                                 pct: float = 25.0, price: float = 0.0,
                                 source: str = "strategy"):
        """Hook for the strategy monitor: buy pct% of cash on entry; on exit
        close ONLY this source's sleeve (what this strategy@timeframe bought),
        so several strategies/timeframes coexist on one account without
        liquidating each other. Never raises."""
        try:
            kw = {"account_id": account_id, "symbol_key": symbol_key,
                  "side": side, "price": price, "source": source,
                  "note": "auto signal"}
            if side == "buy":
                kw["pct"] = pct
            else:
                own = await asyncio.get_running_loop().run_in_executor(
                    None, _source_qty_sync, account_id, symbol_key, source)
                if own <= 1e-10:
                    return {"skipped": "no sleeve for this source"}
                kw["qty"] = own
            r = await cap_sim_order(**kw)
            if r.get("error"):
                log.debug("sim signal skipped: %s", r["error"])
            return r
        except Exception as e:                    # pragma: no cover
            log.debug("sim signal failed: %s", e)
            return {"error": str(e)}

    @capability(
        "markets.sim.equity", http_method="GET", http_path="/markets/sim/equity",
        http_tags=["markets"], memory="off", silent=True,
        description="Equity snapshots for a sim account (hourly + after each "
                    "order). Input: account_id (str!), limit (int=1000). Output: "
                    "{t:[iso], value:[…], cash:[…], orders:[recent]}.",
    )
    async def cap_sim_equity(account_id: str = "", limit: int = 1000,
                             trace_id=None) -> dict:
        if not account_id:
            return {"error": "account_id required"}
        await _ensure_tables()
        loop = asyncio.get_running_loop()

        def _rows():
            conn = _sqlite_conn()
            try:
                rows = conn.execute(
                    "SELECT ts,value,cash FROM mkt_sim_equity WHERE account_id=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (account_id, max(10, min(5000, int(limit))))).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await loop.run_in_executor(None, _rows)
        rows.reverse()
        orders = await loop.run_in_executor(None, _sim_orders_sync, account_id, 60)
        return {"account_id": account_id,
                "t": [r["ts"] for r in rows],
                "value": [r["value"] for r in rows],
                "cash": [r["cash"] for r in rows],
                "orders": orders, "count": len(rows)}

    @capability(
        "markets.sim.reset", http_method="POST", http_path="/markets/sim/reset",
        http_tags=["markets"], memory="on",
        description="Reset a sim account: clears orders + equity history and "
                    "restores starting cash. Input: account_id (str!). Output: {ok}.",
    )
    async def cap_sim_reset(account_id: str = "", trace_id=None) -> dict:
        if not account_id:
            return {"error": "account_id required"}
        await _ensure_tables()

        def _rst():
            conn = _sqlite_conn()
            try:
                r = conn.execute("SELECT start_cash FROM mkt_sim_accounts WHERE id=?",
                                 (account_id,)).fetchone()
                if not r:
                    return False
                conn.execute("DELETE FROM mkt_sim_orders WHERE account_id=?",
                             (account_id,))
                conn.execute("DELETE FROM mkt_sim_equity WHERE account_id=?",
                             (account_id,))
                conn.execute("UPDATE mkt_sim_accounts SET cash=? WHERE id=?",
                             (float(r[0]), account_id))
                conn.commit()
                return True
            finally:
                conn.close()
        ok = await asyncio.get_running_loop().run_in_executor(None, _rst)
        if not ok:
            return {"error": "no such sim account", "id": account_id}
        await emit_event({"type": "markets.sim", "stage": "reset", "id": account_id})
        return {"ok": True, "id": account_id}

    @capability(
        "markets.sim.delete", http_method="POST", http_path="/markets/sim/delete",
        http_tags=["markets"], memory="on",
        description="Delete a sim account and its history. Input: account_id "
                    "(str!). Output: {ok}.",
    )
    async def cap_sim_delete(account_id: str = "", trace_id=None) -> dict:
        if not account_id:
            return {"error": "account_id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_sim_orders WHERE account_id=?",
                             (account_id,))
                conn.execute("DELETE FROM mkt_sim_equity WHERE account_id=?",
                             (account_id,))
                conn.execute("DELETE FROM mkt_sim_accounts WHERE id=?", (account_id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        return {"ok": True, "id": account_id}

    async def _sim_snapshot_tick():
        """Hourly: snapshot every sim account + keep the macro provider hooked."""
        try:
            _register_macro_provider()
            await _ensure_tables()
            rows = await asyncio.get_running_loop().run_in_executor(
                None, _sim_accounts_sync)
            for acct in rows:
                await _sim_snapshot(acct["id"])
        except Exception as e:                    # pragma: no cover
            log.debug("sim snapshot tick: %s", e)

    schedule(_sim_snapshot_tick, 3600, "mkt_sim_snapshot")

    # ── Strategy version history (never lose a tuned setup) ──────────────────

    @capability(
        "markets.strategy.versions", http_method="GET",
        http_path="/markets/strategy/versions", http_tags=["markets"],
        memory="off", silent=True,
        description="Version history of a strategy's spec — a snapshot is taken "
                    "automatically every time the spec is overwritten (manual "
                    "edit, autotune adopt, evolve promotion). Input: id (str!). "
                    "Output: {versions:[{index,name,kind,saved_at,spec}]}.",
    )
    async def cap_strategy_versions(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        stack = await asyncio.get_running_loop().run_in_executor(
            None, _kv_get_sync, f"stratver:{id}") or []
        return {"id": id,
                "versions": [{"index": i, **v} for i, v in enumerate(stack)
                             if isinstance(v, dict)],
                "count": len(stack)}

    @capability(
        "markets.strategy.revert", http_method="POST",
        http_path="/markets/strategy/revert", http_tags=["markets"], memory="on",
        description="Restore a strategy to one of its snapshots (the current "
                    "spec is snapshotted first, so a revert is itself "
                    "reversible). Input: id (str!), index (int=0 — 0 is the most "
                    "recent snapshot). Output: {ok, id, restored_from}.",
    )
    async def cap_strategy_revert(id: str = "", index: int = 0, trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        lab = _mlab()
        await _ensure_tables()
        stack = await asyncio.get_running_loop().run_in_executor(
            None, _kv_get_sync, f"stratver:{id}") or []
        idx = max(0, int(index))
        if idx >= len(stack):
            return {"error": f"no snapshot at index {idx}", "count": len(stack)}
        snap = stack[idx]
        r = await lab.cap_strategy_save(
            name=snap.get("name") or "restored", spec=snap.get("spec"),
            kind=snap.get("kind") or (snap.get("spec") or {}).get("kind") or "rule",
            id=id)
        if r.get("error"):
            return r
        return {"ok": True, "id": id, "restored_from": snap.get("saved_at")}

    # ── Background TRADER director — like the dream director, for markets ────
    #
    # A deterministic core the LLM only STEERS: every tick it (1) snapshots the
    # market, (2) advances a rolling strategies×assets results GRID (structured
    # background backtesting incl. a best-pair layered composite per asset),
    # (3) executes fresh signals — sim mode trades ONLY the configured sim
    # account (per-strategy sleeves); real mode NEVER touches sim accounts and
    # only raises alerts / (opt-in) records ledger transactions. The optional
    # LLM steer adjusts config within hard bounds; it never places trades and
    # only ever sees the active mode's account data.

    TRADER_CFG_KEY = "studio:trader:cfg"
    TRADER_LOG_KEY = "studio:trader:log"
    TRADER_GRID_KEY = "studio:trader:grid"
    TRADER_DEFAULTS = {
        "enabled": False, "interval_min": 60, "mode": "sim",
        "sim_account_id": "", "strategy_ids": [], "assets": [], "tf": "1d",
        "per_trade_pct": 15.0, "max_positions": 6, "min_metric": 0.5,
        "metric": "sharpe", "grid_cells_per_tick": 3, "grid_limit": 4000,
        "real_autolog": False, "llm_steer": False, "steer_every": 6,
        "tick_count": 0, "last_run": "", "last_brief": "",
    }

    async def _trader_cfg() -> dict:
        cfg = dict(TRADER_DEFAULTS)
        cur = await asyncio.get_running_loop().run_in_executor(
            None, _kv_get_sync, TRADER_CFG_KEY)
        if isinstance(cur, dict):
            cfg.update(cur)
        return cfg

    async def _trader_save_cfg(cfg: dict):
        await asyncio.get_running_loop().run_in_executor(
            None, _kv_set_sync, TRADER_CFG_KEY, cfg)

    async def _trader_log(kind: str, msg: str, extra: dict = None):
        loop = asyncio.get_running_loop()
        logl = await loop.run_in_executor(None, _kv_get_sync, TRADER_LOG_KEY) or []
        if not isinstance(logl, list):
            logl = []
        logl.insert(0, {"ts": now_iso(), "kind": kind, "msg": msg,
                        **(extra or {})})
        await loop.run_in_executor(None, lambda: _kv_set_sync(TRADER_LOG_KEY,
                                                              logl[:100]))
        await emit_event({"type": "markets.trader", "stage": kind,
                          "message": msg, **(extra or {})})

    def _t_metric(stats: dict, metric: str):
        if not stats:
            return None
        return stats.get(metric)

    async def _trader_tick_impl(force: bool = False) -> dict:
        cfg = await _trader_cfg()
        if not cfg.get("enabled") and not force:
            return {"skipped": "disabled"}
        lab, md, ma, mc = _mlab(), _md(), _ma(), _mc()
        if not (lab and md and ma and mc):
            return {"error": "markets modules unavailable"}
        loop = asyncio.get_running_loop()
        mode = "real" if cfg.get("mode") == "real" else "sim"
        summary = {"mode": mode, "grid_cells": 0, "trades": [], "signals": 0}

        # 1) monitor the market
        try:
            ov = await cap_markets_overview()
            gs = {g["name"]: g for g in ov.get("groups") or []}
            sect = gs.get("Sectors") or {}
            brief = (f"breadth(sectors 1d) {sect.get('breadth_1d')}% · "
                     f"median {sect.get('median_1d')}%")
            cfg["last_brief"] = brief
        except Exception:
            brief = ""

        # resolve strategies + assets
        sids = list(cfg.get("strategy_ids") or [])
        if not sids:
            def _acc():
                conn = _sqlite_conn()
                try:
                    return [r[0] for r in conn.execute(
                        "SELECT id FROM mkt_strategies WHERE status='accepted'"
                    ).fetchall()]
                finally:
                    conn.close()
            sids = await loop.run_in_executor(None, _acc)
        strats = []
        for sid in sids[:8]:
            row = await loop.run_in_executor(None, lab._strategy_full_sync, sid)
            if row:
                strats.append(row)
        assets = list(cfg.get("assets") or [])
        if not assets:
            rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
            assets = [r["id"] for r in rows
                      if r.get("exchange") not in ("macro", "dyn", "custom")][:8]
        tf = cfg.get("tf") or "1d"
        dss = []
        for a in assets:
            prov, sym = (a.split(":", 1) + [""])[:2] if ":" in a else ("binance", a)
            dss.append((a, md._dataset_id(prov, sym, tf)))

        # 2) rolling results grid (+ layered best-pair per asset)
        grid = await loop.run_in_executor(None, _kv_get_sync, TRADER_GRID_KEY) or {}
        cells = grid.get("cells") or {}
        cursor = int(grid.get("cursor") or 0)
        pairs = [(s, a, ds) for s in strats for (a, ds) in dss]
        arrs_cache: Dict[str, object] = {}
        if pairs:
            n_do = max(1, min(8, int(cfg.get("grid_cells_per_tick") or 3)))
            for k in range(n_do):
                s, a, ds = pairs[(cursor + k) % len(pairs)]
                try:
                    if ds not in arrs_cache:
                        bars = await md.get_bars(ds, int(cfg.get("grid_limit") or 4000))
                        arrs_cache[ds] = ma.bars_to_arrays(bars) if len(bars) >= 200 else None
                    arr = arrs_cache[ds]
                    if arr is None:
                        continue
                    e, x, se, sx = await lab._spec_signals(arr, s["spec"])
                    res = await loop.run_in_executor(None, lab.run_backtest,
                                                     arr, e, x, s["spec"], se, sx)
                    st2 = res["stats"]
                    cells[f"{s['id']}|{a}"] = {
                        "strategy": s.get("name"), "asset": a,
                        "metric": _t_metric(st2, cfg.get("metric") or "sharpe"),
                        "ret_pct": st2.get("total_return_pct"),
                        "dd_pct": st2.get("max_drawdown_pct"),
                        "trades": st2.get("trades"), "ts": now_iso()}
                    summary["grid_cells"] += 1
                except Exception as e2:
                    cells[f"{s['id']}|{a}"] = {"error": str(e2)[:120], "ts": now_iso()}
            cursor = (cursor + n_do) % len(pairs)
            # layered composite: best two strategies per asset, weighted-fused
            layered = grid.get("layered") or {}
            for (a, ds) in dss[:4]:
                ranked = sorted(
                    [(cells[f"{s['id']}|{a}"], s) for s in strats
                     if isinstance(cells.get(f"{s['id']}|{a}"), dict)
                     and cells[f"{s['id']}|{a}"].get("metric") is not None],
                    key=lambda t2: -(t2[0]["metric"] or -9e9))
                if len(ranked) >= 2 and arrs_cache.get(ds) is not None:
                    try:
                        fspec = {"kind": "fused", "combine": "weighted",
                                 "weights": [0.6, 0.4], "enter_threshold": 0.5,
                                 "exit_threshold": 0.3,
                                 "members": [ranked[0][1]["spec"], ranked[1][1]["spec"]],
                                 "fee_bps": 10, "slippage_bps": 5}
                        e, x, se, sx = await lab._spec_signals(arrs_cache[ds], fspec)
                        res = await loop.run_in_executor(
                            None, lab.run_backtest, arrs_cache[ds], e, x, fspec, se, sx)
                        layered[a] = {
                            "members": [ranked[0][1].get("name"), ranked[1][1].get("name")],
                            "metric": _t_metric(res["stats"], cfg.get("metric") or "sharpe"),
                            "ret_pct": res["stats"].get("total_return_pct"),
                            "ts": now_iso()}
                    except Exception:
                        pass
            grid = {"cells": cells, "cursor": cursor, "layered": layered,
                    "updated_at": now_iso()}
            await loop.run_in_executor(None, lambda: _kv_set_sync(TRADER_GRID_KEY, grid))

        # 3) execute fresh signals — DETERMINISTIC, mode-isolated
        sim_id = cfg.get("sim_account_id") or ""
        min_metric = float(cfg.get("min_metric") or 0)
        for s in strats:
            for (a, ds) in dss:
                cell = cells.get(f"{s['id']}|{a}") or {}
                if cell.get("metric") is None or cell["metric"] < min_metric:
                    continue                     # only trade proven cells
                try:
                    bars = await md.get_bars(ds, 400)
                    if len(bars) < 100:
                        continue
                    arr = ma.bars_to_arrays(bars)
                    e, x, se, sx = await lab._spec_signals(arr, s["spec"])
                except Exception:
                    continue
                sig = ("buy" if bool(e[-1]) else
                       "sell" if bool(x[-1]) else
                       "short" if (se is not None and bool(se[-1])) else None)
                if not sig:
                    continue
                summary["signals"] += 1
                px = float(arr["c"][-1])
                if mode == "sim":
                    if not sim_id:
                        continue
                    if sig == "short":
                        continue                 # spot sim can't short
                    if sig == "buy":
                        val = await _sim_value(await loop.run_in_executor(
                            None, _sim_account_sync, sim_id) or {"cash": 0, "id": sim_id})
                        openpos = len([p for p in val.get("positions") or []
                                       if (p.get("qty") or 0) > 0])
                        if openpos >= int(cfg.get("max_positions") or 6):
                            continue
                    r = await sim_execute_signal(
                        sim_id, a, sig, float(cfg.get("per_trade_pct") or 15),
                        px, f"trader:{s['id']}")
                    if r.get("ok"):
                        summary["trades"].append({"mode": "sim", "side": sig,
                                                  "asset": a, "strategy": s.get("name")})
                        await _trader_log("trade",
                                          f"[sim] {sig} {a} via {s.get('name')} @ {px}")
                else:
                    await _trader_log("signal",
                                      f"[real] {sig.upper()} signal: {a} via "
                                      f"{s.get('name')} @ {px} — "
                                      + ("logging to ledger" if cfg.get("real_autolog")
                                         else "review & record manually"))
                    if cfg.get("real_autolog") and sig in ("buy", "sell"):
                        try:
                            notional = 0.0  # real mode sizes by per_trade_pct of ledger value
                            pv = await lab.cap_portfolio_positions()
                            total = float((pv.get("totals") or {}).get("value") or 0)
                            notional = total * float(cfg.get("per_trade_pct") or 15) / 100.0
                            if notional > 0 and px > 0:
                                await lab.cap_portfolio_tx_add(
                                    symbol_key=a, side=sig,
                                    qty=round(notional / px, 8), price=px,
                                    note=f"trader:{s.get('name')}")
                                summary["trades"].append({"mode": "real", "side": sig,
                                                          "asset": a,
                                                          "strategy": s.get("name")})
                        except Exception as e3:
                            log.debug("trader real log: %s", e3)

        # 4) optional LLM steering (bounded, mode-scoped view, never trades)
        cfg["tick_count"] = int(cfg.get("tick_count") or 0) + 1
        if cfg.get("llm_steer") and cfg["tick_count"] % max(1, int(cfg.get("steer_every") or 6)) == 0:
            try:
                if mode == "sim" and sim_id:
                    acct = await loop.run_in_executor(None, _sim_account_sync, sim_id)
                    acct_view = await _sim_value(acct) if acct else {}
                    acct_txt = f"sim account value {acct_view.get('value')} ret {acct_view.get('ret_pct')}%"
                else:
                    pv = await lab.cap_portfolio_positions()
                    acct_txt = f"real book value {(pv.get('totals') or {}).get('value')}"
                top = sorted([c for c in cells.values()
                              if isinstance(c, dict) and c.get("metric") is not None],
                             key=lambda c: -c["metric"])[:6]
                prompt = (
                    "You steer (never trade) a deterministic markets trader.\n"
                    f"Mode: {mode} (you can ONLY see this mode's account).\n"
                    f"Market: {brief}\nAccount: {acct_txt}\n"
                    "Results grid (best cells): " +
                    "; ".join(f"{c['strategy']}×{c['asset']}={c['metric']}" for c in top) +
                    "\nReply ONLY JSON with any of: {\"per_trade_pct\": 5-50, "
                    "\"min_metric\": 0-3, \"max_positions\": 1-12, "
                    "\"note\": \"<one line>\"}")
                raw = await ollama_generate(prompt, json_mode=True,
                                            job_type="analysis", timeout=90)
                parsed = ma.parse_llm_json(raw) or {}
                changed = {}
                if parsed.get("per_trade_pct") is not None:
                    cfg["per_trade_pct"] = max(5.0, min(50.0, float(parsed["per_trade_pct"])))
                    changed["per_trade_pct"] = cfg["per_trade_pct"]
                if parsed.get("min_metric") is not None:
                    cfg["min_metric"] = max(0.0, min(3.0, float(parsed["min_metric"])))
                    changed["min_metric"] = cfg["min_metric"]
                if parsed.get("max_positions") is not None:
                    cfg["max_positions"] = max(1, min(12, int(parsed["max_positions"])))
                    changed["max_positions"] = cfg["max_positions"]
                if changed or parsed.get("note"):
                    await _trader_log("steer",
                                      f"LLM steer: {json.dumps(changed)} — "
                                      f"{str(parsed.get('note') or '')[:140]}")
            except Exception as e4:
                log.debug("trader steer: %s", e4)

        cfg["last_run"] = now_iso()
        await _trader_save_cfg(cfg)
        await _trader_log("tick",
                          f"tick #{cfg['tick_count']} [{mode}] — "
                          f"{summary['grid_cells']} grid cells, "
                          f"{summary['signals']} signals, "
                          f"{len(summary['trades'])} trades", summary)
        return {"ok": True, **summary}

    _TRADER_RUNNING = {"flag": False}

    async def _trader_sched_tick():
        if _TRADER_RUNNING["flag"]:
            return
        try:
            cfg = await _trader_cfg()
            if not cfg.get("enabled"):
                return
            last = cfg.get("last_run") or ""
            due = True
            if last:
                try:
                    from datetime import datetime as _dt
                    t0 = _dt.fromisoformat(last.replace("Z", ""))
                    due = (time.time() - t0.timestamp()) >= \
                        max(5, int(cfg.get("interval_min") or 60)) * 60
                except Exception:
                    due = True
            if due:
                _TRADER_RUNNING["flag"] = True
                try:
                    await _trader_tick_impl()
                finally:
                    _TRADER_RUNNING["flag"] = False
        except Exception as e:
            _TRADER_RUNNING["flag"] = False
            log.debug("trader sched: %s", e)

    schedule(_trader_sched_tick, 60, "mkt_trader")

    @capability(
        "markets.trader.status", http_method="GET", http_path="/markets/trader/status",
        http_tags=["markets"], memory="off", silent=True,
        description="Background trader director status: config, market brief, the "
                    "rolling strategies×assets results grid (incl. layered "
                    "composites), and the recent action log. Output: {config, "
                    "grid:{cells,layered}, log:[…], running}.",
    )
    async def cap_trader_status(trace_id=None) -> dict:
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        cfg = await _trader_cfg()
        grid = await loop.run_in_executor(None, _kv_get_sync, TRADER_GRID_KEY) or {}
        logl = await loop.run_in_executor(None, _kv_get_sync, TRADER_LOG_KEY) or []
        return {"config": cfg, "grid": grid, "log": logl[:40],
                "running": _TRADER_RUNNING["flag"]}

    @capability(
        "markets.trader.config.set", http_method="POST",
        http_path="/markets/trader/config/set", http_tags=["markets"], memory="on",
        schema=enum_schema(mode=["sim", "real"]),
        description="Configure the background trader. SIM mode trades ONLY the "
                    "configured sim account (per-strategy sleeves) and cannot see "
                    "the real book; REAL mode never touches sim accounts and only "
                    "raises signal alerts unless real_autolog=true (then signals "
                    "are recorded as ledger transactions). Input: any of enabled "
                    "(bool), mode (sim|real), sim_account_id, strategy_ids (list — "
                    "empty = all accepted), assets (list — empty = watchlist), tf, "
                    "interval_min, per_trade_pct, max_positions, min_metric, "
                    "metric, grid_cells_per_tick, real_autolog (bool), llm_steer "
                    "(bool), steer_every. Output: {ok, config}.",
    )
    async def cap_trader_config(config=None, trace_id=None, **fields) -> dict:
        await _ensure_tables()
        cfg = await _trader_cfg()
        patch = {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except Exception:
                config = None
        if isinstance(config, dict):
            patch.update(config)
        patch.update({k: v for k, v in fields.items() if v is not None})
        for k, v in patch.items():
            if k in TRADER_DEFAULTS and k not in ("tick_count", "last_run", "last_brief"):
                cfg[k] = v
        if cfg.get("mode") == "sim" and not cfg.get("sim_account_id"):
            rows = await asyncio.get_running_loop().run_in_executor(
                None, _sim_accounts_sync)
            if rows:
                cfg["sim_account_id"] = rows[0]["id"]
        await _trader_save_cfg(cfg)
        return {"ok": True, "config": cfg}

    @capability(
        "markets.trader.tick", http_method="POST", http_path="/markets/trader/tick",
        http_tags=["markets"], memory="on",
        description="Run one trader iteration now (monitor → grid backtests → "
                    "signal execution → optional steer). Output: tick summary.",
    )
    async def cap_trader_tick(trace_id=None) -> dict:
        if _TRADER_RUNNING["flag"]:
            return {"error": "a tick is already running"}
        _TRADER_RUNNING["flag"] = True
        try:
            return await _trader_tick_impl(force=True)
        finally:
            _TRADER_RUNNING["flag"] = False

    # ── Saved layouts ────────────────────────────────────────────────────────

    @capability(
        "markets.layout.save", http_method="POST", http_path="/markets/layout/save",
        http_tags=["markets"], memory="on",
        description="Save a named studio chart/tile layout (tiles, assets, "
                    "indicators, compare layers, settings). Input: name (str!), "
                    "data (object!), kind (str='studio'). Output: {ok, key}.",
    )
    async def cap_layout_save(name: str = "", data=None, kind: str = "studio",
                              trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return {"error": "data must be a JSON object"}
        if not isinstance(data, dict):
            return {"error": "data object required"}
        await _ensure_tables()
        import re as _re
        slugn = _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "layout"
        key = f"studio:layout:{slugn}"
        await asyncio.get_running_loop().run_in_executor(
            None, _kv_set_sync, key,
            {"name": name.strip(), "kind": kind or "studio", "data": data})
        return {"ok": True, "key": key}

    @capability(
        "markets.layout.list", http_method="GET", http_path="/markets/layout/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List saved studio layouts. Output: {layouts:[{key,name,kind,"
                    "data,updated_at}]}.",
    )
    async def cap_layout_list(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _kv_scan_sync, "studio:layout:")
        out = [{"key": r["key"], "updated_at": r["updated_at"],
                **(r["value"] or {})} for r in rows]
        return {"layouts": out, "count": len(out)}

    @capability(
        "markets.layout.delete", http_method="POST",
        http_path="/markets/layout/delete", http_tags=["markets"], memory="on",
        description="Delete a saved layout. Input: key (str!). Output: {ok}.",
    )
    async def cap_layout_delete(key: str = "", trace_id=None) -> dict:
        if not key or not key.startswith("studio:layout:"):
            return {"error": "key required (studio:layout:…)"}
        await _ensure_tables()
        await asyncio.get_running_loop().run_in_executor(None, _kv_del_sync, key)
        return {"ok": True, "key": key}

    # ── Panel route + registration ───────────────────────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/markets/studio/panel", include_in_schema=False)
    async def _markets_studio_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "markets_studio_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>markets_studio_panel.html not found</p>")

    STUDIO_CAPS = [
        # studio layer
        "markets.strategy.library", "markets.strategy.from_template",
        "markets.analysis.trendfit", "markets.analysis.pivots",
        "markets.backtest.analyze",
        "markets.backtest.autotune", "markets.backtest.autotune_status",
        "markets.backtest.batch", "markets.backtest.batch_status",
        "markets.ml.walkforward",
        "markets.infographic.save", "markets.infographic.list",
        "markets.infographic.delete",
        "markets.project.asset", "markets.project.portfolio",
        "markets.portfolio.optimize", "markets.rotation.scan",
        "markets.dynamics.fetch", "markets.dynamics.snapshot",
        "markets.wsb.scan", "markets.news.feed", "markets.sentiment.to_series",
        "markets.sim.templates",
        "markets.strategy.versions", "markets.strategy.revert",
        "markets.trader.status", "markets.trader.config.set", "markets.trader.tick",
        "markets.evolve.start", "markets.evolve.stop", "markets.evolve.status",
        "markets.evolve.history", "markets.evolve.config.set", "markets.evolve.tick",
        "markets.baseline.list", "markets.baseline.ensure", "markets.overview",
        "markets.events.detect", "markets.events.apply",
        "markets.macro.catalog", "markets.macro.fetch",
        "markets.sim.create", "markets.sim.list", "markets.sim.order",
        "markets.sim.equity", "markets.sim.reset", "markets.sim.delete",
        "markets.layout.save", "markets.layout.list", "markets.layout.delete",
        # existing layers the studio drives
        "markets.lookup", "markets.bars", "markets.quotes", "markets.fetch",
        "markets.jobs", "markets.watchlist.list", "markets.watchlist.add",
        "markets.update_now", "markets.history.audit", "markets.history.repair",
        "markets.indicators", "markets.indicator_config.get",
        "markets.indicator_config.set", "markets.indicator.custom.save",
        "markets.indicator.custom.list", "markets.indicator.custom.delete",
        "markets.indicator.custom.test",
        "markets.annotate.add", "markets.annotate.list", "markets.annotate.remove",
        "markets.ml.create", "markets.ml.list", "markets.ml.train",
        "markets.ml.predict", "markets.ml.series",
        "markets.strategy.save", "markets.strategy.list", "markets.strategy.delete",
        "markets.strategy.accept", "markets.strategy.archive",
        "markets.monitor.status", "markets.alerts.list", "markets.alerts.ack",
        "markets.backtest.run", "markets.backtest.list", "markets.backtest.get",
        "markets.backtest.signals", "markets.backtest.sweep",
        "markets.backtest.sweep_status",
    ]

    # NOTE: no register_ui here — the studio is a sub-tab of the single merged
    # "Markets" tab registered by markets_capabilities.py (which serves both
    # /markets/panel and /markets/studio/panel in one shell). STUDIO_CAPS is
    # exported for that registration + the loop profiles.

    log.info("markets studio capabilities loaded (%d library strategies, "
             "%d macro series, %d baseline assets)",
             len(STRATEGY_LIBRARY), len(MACRO_CATALOG), len(BASELINE_ASSETS))

elif _CAP_AVAILABLE and not HAS_NUMPY:          # pragma: no cover
    log.warning("markets studio disabled — numpy not installed")
