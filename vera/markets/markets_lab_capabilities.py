"""
markets_lab_capabilities.py — Predictive ML tools, backtesting & portfolio
==========================================================================

The quant layer of the Markets workbench:

• **ML tools** (`markets.ml.*`) — user/Vera-defined predictive models over any
  stored bar series: pick features (returns, RSI, MACD histogram, stochastics,
  Bollinger %B, ATR, volume z-score, EMA ratio, ROC), a horizon and a model
  kind (gradient boosting / random forest / logistic / ridge). Training runs in
  the background with walk-forward (TimeSeriesSplit) validation; metrics
  (accuracy/F1 or MAE/R², plus signal hit-rate and Sharpe) are stored on the
  model row so Vera can inspect and *tweak* hyperparameters via
  `markets.ml.update` and retrain.

• **Backtesting** (`markets.strategy.*`, `markets.backtest.*`) — a small JSON
  rule DSL (indicator/price/ML-signal comparisons and crossovers) drives a
  bar-by-bar simulator with fees, slippage, stop-loss/take-profit. Results
  (equity curve, trades, CAGR/Sharpe/Sortino/max-drawdown/win-rate/profit
  factor vs buy & hold) are persisted and pushed to the UI.

• **Portfolio** (`markets.portfolio.*`) — transaction ledger (FIFO lots) across
  crypto/stocks/custom assets with live valuation from stored bars, realized +
  unrealized P&L, allocation and a daily value history.

Engine functions (`run_backtest`, `build_features`) are pure and importable
standalone for tests.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import pickle
import sys
import time
import uuid
from typing import Callable, Dict, List, Optional

log = logging.getLogger("vera.markets.lab")

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:                              # pragma: no cover
    HAS_NUMPY = False

try:
    from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor, \
        RandomForestClassifier, RandomForestRegressor
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, r2_score
    HAS_SKLEARN = True
except Exception:                              # pragma: no cover
    HAS_SKLEARN = False

try:
    import backtrader as _bt
    HAS_BACKTRADER = True
except Exception:                              # pragma: no cover
    HAS_BACKTRADER = False

try:
    import pandas as _pd
    HAS_PANDAS = True
except Exception:                              # pragma: no cover
    HAS_PANDAS = False

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, enum_schema, now_iso, schedule,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.markets.lab").warning("markets lab caps unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Feature engineering (pure — needs an indicator `compute` fn injected)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_FEATURES = ["ret_1", "ret_5", "ret_10", "rsi", "macd_hist", "stoch_k",
                    "bb_pctb", "atr_norm", "vol_z", "ema_ratio", "roc"]

MODEL_KINDS = ["gbt", "rf", "logreg", "ridge"]
ML_TASKS = ["classify", "regress"]


def _pct_ret(c, n):
    out = np.full(len(c), np.nan)
    if len(c) > n:
        prev = c[:-n]
        out[n:] = np.divide(c[n:] - prev, prev,
                            out=np.zeros(len(c) - n), where=(prev != 0))
    return out


def build_features(arr: Dict[str, "np.ndarray"], features: List[str],
                   compute: Callable) -> ("np.ndarray", List[str]):
    """Feature matrix aligned to arr['t']. `compute(arr, kind, params)` is the
    indicator engine from markets_analysis_capabilities."""
    c, v, t = arr["c"], arr["v"], arr["t"]
    cols, names = [], []

    def add(name, series):
        cols.append(np.asarray(series, dtype=np.float64))
        names.append(name)

    for f in features:
        f = str(f).strip().lower()
        if f.startswith("ret_"):
            add(f, _pct_ret(c, max(1, int(f.split("_")[1]))))
        elif f == "rsi":
            add("rsi", compute(arr, "rsi", {})["rsi"] / 100.0)
        elif f == "macd_hist":
            hist = compute(arr, "macd", {})["macd_hist"]
            add("macd_hist", np.divide(hist, c, out=np.full(len(c), np.nan), where=(c != 0)))
        elif f == "stoch_k":
            add("stoch_k", compute(arr, "stoch", {})["stoch_k"] / 100.0)
        elif f == "bb_pctb":
            bb = compute(arr, "bbands", {})
            rng = bb["bb_upper"] - bb["bb_lower"]
            add("bb_pctb", np.divide(c - bb["bb_lower"], rng,
                                     out=np.full(len(c), np.nan), where=(rng != 0)))
        elif f == "atr_norm":
            atr = compute(arr, "atr", {})["atr"]
            add("atr_norm", np.divide(atr, c, out=np.full(len(c), np.nan), where=(c != 0)))
        elif f == "vol_z":
            m = np.full(len(v), np.nan); s = np.full(len(v), np.nan)
            n = 20
            for i in range(n - 1, len(v)):
                w = v[i - n + 1: i + 1]
                m[i] = np.mean(w); s[i] = np.std(w)
            add("vol_z", np.divide(v - m, s, out=np.zeros(len(v)), where=(s != 0)))
        elif f == "ema_ratio":
            e20 = compute(arr, "ema", {"n": 20})["ema20"]
            e50 = compute(arr, "ema", {"n": 50})["ema50"]
            add("ema_ratio", np.divide(e20, e50, out=np.full(len(c), np.nan),
                                       where=(e50 != 0)) - 1.0)
        elif f == "roc":
            add("roc", compute(arr, "roc", {})["roc"] / 100.0)
        elif f == "dow":
            add("dow", (np.asarray(t) // 86400 + 4) % 7 / 6.0)
        # unknown feature names are skipped silently (forward compat)
    if not cols:
        raise ValueError("no valid features")
    return np.column_stack(cols), names


def infer_bars_per_year(t: "np.ndarray") -> float:
    if len(t) < 3:
        return 365.0
    dt = float(np.median(np.diff(t)))
    return (365.0 * 86400.0) / max(1.0, dt)


# ─────────────────────────────────────────────────────────────────────────────
# Backtest engine (pure)
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(arr: Dict[str, "np.ndarray"], entry: "np.ndarray",
                 exit_: "np.ndarray", opts: dict = None,
                 short_entry: "np.ndarray" = None,
                 short_exit: "np.ndarray" = None) -> dict:
    """Bar-by-bar long/short simulator. Signal arrays are booleans evaluated on
    bar close; fills happen at the NEXT bar open (with slippage + fees).
    Long-only when `short_entry` is None (back-compat). Shorts model a cash
    account: sale proceeds are credited on entry, the liability is marked to
    market (`equity = cash + qty·px − short_qty·px`), covered on exit; SL/TP
    invert for the short side. An opposing entry signal closes the open
    position (no same-bar flip). opts: fee_bps, slippage_bps, size_pct,
    stop_loss_pct, take_profit_pct."""
    o, h, l, c, t = arr["o"], arr["h"], arr["l"], arr["c"], arr["t"]
    n = len(c)
    opts = opts or {}
    fee = float(opts.get("fee_bps", 10)) / 10_000.0
    slip = float(opts.get("slippage_bps", 5)) / 10_000.0
    size = max(1.0, min(100.0, float(opts.get("size_pct", 100)))) / 100.0
    sl = float(opts.get("stop_loss_pct", 0)) / 100.0
    tp = float(opts.get("take_profit_pct", 0)) / 100.0
    # leverage multiplies deployed notional (cash can go negative = borrowed;
    # equity is marked to market). equity ≤ 0 → liquidation: flat, game over.
    lev = max(1.0, min(10.0, float(opts.get("leverage", 1) or 1)))
    has_short = short_entry is not None
    if short_entry is None:
        short_entry = np.zeros(n, dtype=bool)
    if short_exit is None:
        short_exit = np.zeros(n, dtype=bool)

    equity = np.ones(n)
    cash, qty, sqty, entry_px = 1.0, 0.0, 0.0, 0.0
    liquidated = False
    pos = 0                                   # 0 flat | 1 long | -1 short
    trades: List[dict] = []
    cur: Optional[dict] = None
    pending: Optional[str] = None            # enter|exit|enter_short|exit_short
    bars_in_pos = 0

    for i in range(n):
        px_open = o[i] if o[i] > 0 else c[i]
        # 1) execute pending order at this bar's open
        if pending == "enter" and pos == 0:
            fill = px_open * (1 + slip)
            spend = max(0.0, cash) * size * lev
            qty = (spend * (1 - fee)) / fill if fill > 0 else 0.0
            cash -= spend
            entry_px = fill
            pos, bars_in_pos = 1, 0
            cur = {"entry_t": int(t[i]), "entry_px": round(float(fill), 8),
                   "side": "long", "qty": round(float(qty), 8),
                   "notional": round(float(qty * fill), 2)}
        elif pending == "exit" and pos == 1:
            fill = px_open * (1 - slip)
            cash += qty * fill * (1 - fee)
            _close_trade(cur, trades, int(t[i]), fill, bars_in_pos)
            qty, pos, cur = 0.0, 0, None
        elif pending == "enter_short" and pos == 0:
            fill = px_open * (1 - slip)
            sqty = (max(0.0, cash) * size * lev) / fill if fill > 0 else 0.0
            cash += sqty * fill * (1 - fee)
            entry_px = fill
            pos, bars_in_pos = -1, 0
            cur = {"entry_t": int(t[i]), "entry_px": round(float(fill), 8),
                   "side": "short", "qty": round(float(sqty), 8),
                   "notional": round(float(sqty * fill), 2)}
        elif pending == "exit_short" and pos == -1:
            fill = px_open * (1 + slip)
            cash -= sqty * fill * (1 + fee)
            _close_trade(cur, trades, int(t[i]), fill, bars_in_pos)
            sqty, pos, cur = 0.0, 0, None
        elif pending == "flip_short" and pos == 1:
            # close the long and open the short at the same bar's open — cross
            # signals fire on one bar only, so without a flip an always-in
            # long/short strategy could never actually enter its short leg
            fill = px_open * (1 - slip)
            cash += qty * fill * (1 - fee)
            _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="flip")
            qty = 0.0
            sqty = (max(0.0, cash) * size * lev) / fill if fill > 0 else 0.0
            cash += sqty * fill * (1 - fee)
            entry_px = fill
            pos, bars_in_pos = -1, 0
            cur = {"entry_t": int(t[i]), "entry_px": round(float(fill), 8),
                   "side": "short", "qty": round(float(sqty), 8),
                   "notional": round(float(sqty * fill), 2)}
        elif pending == "flip_long" and pos == -1:
            fill = px_open * (1 + slip)
            cash -= sqty * fill * (1 + fee)
            _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="flip")
            sqty = 0.0
            spend = max(0.0, cash) * size * lev
            qty = (spend * (1 - fee)) / fill if fill > 0 else 0.0
            cash -= spend
            entry_px = fill
            pos, bars_in_pos = 1, 0
            cur = {"entry_t": int(t[i]), "entry_px": round(float(fill), 8),
                   "side": "long", "qty": round(float(qty), 8),
                   "notional": round(float(qty * fill), 2)}
        pending = None

        # 2) intrabar stop-loss / take-profit
        if pos == 1 and (sl > 0 or tp > 0):
            stop_px = entry_px * (1 - sl) if sl > 0 else None
            take_px = entry_px * (1 + tp) if tp > 0 else None
            if stop_px is not None and l[i] <= stop_px:
                fill = stop_px * (1 - slip)
                cash += qty * fill * (1 - fee)
                _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="stop")
                qty, pos, cur = 0.0, 0, None
            elif take_px is not None and h[i] >= take_px:
                fill = take_px * (1 - slip)
                cash += qty * fill * (1 - fee)
                _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="take")
                qty, pos, cur = 0.0, 0, None
        elif pos == -1 and (sl > 0 or tp > 0):
            stop_px = entry_px * (1 + sl) if sl > 0 else None
            take_px = entry_px * (1 - tp) if tp > 0 else None
            if stop_px is not None and h[i] >= stop_px:
                fill = stop_px * (1 + slip)
                cash -= sqty * fill * (1 + fee)
                _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="stop")
                sqty, pos, cur = 0.0, 0, None
            elif take_px is not None and l[i] <= take_px:
                fill = take_px * (1 + slip)
                cash -= sqty * fill * (1 + fee)
                _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="take")
                sqty, pos, cur = 0.0, 0, None

        # 3) signals on close set next bar's action (opposing entry = flip)
        if pos == 1:
            bars_in_pos += 1
            if has_short and bool(short_entry[i]):
                pending = "flip_short"
            elif bool(exit_[i]):
                pending = "exit"
        elif pos == -1:
            bars_in_pos += 1
            if bool(entry[i]):
                pending = "flip_long"
            elif bool(short_exit[i]):
                pending = "exit_short"
        else:
            if bool(entry[i]):
                pending = "enter"
            elif has_short and bool(short_entry[i]):
                pending = "enter_short"

        equity[i] = cash + qty * c[i] - sqty * c[i]
        if lev > 1.0 and equity[i] <= 0:
            # margin wiped out — liquidated, flat and done
            equity[i:] = 0.0
            if cur:
                fill = c[i]
                cash = 0.0
                _close_trade(cur, trades, int(t[i]), fill, bars_in_pos, reason="liquidated")
            qty, sqty, pos, cur, pending = 0.0, 0.0, 0, None, None
            liquidated = True
            break

    # force-close at the end for stats
    if pos == 1 and cur:
        fill = c[-1] * (1 - slip)
        cash += qty * fill * (1 - fee)
        _close_trade(cur, trades, int(t[-1]), fill, bars_in_pos, reason="end")
        equity[-1] = cash
    elif pos == -1 and cur:
        fill = c[-1] * (1 + slip)
        cash -= sqty * fill * (1 + fee)
        _close_trade(cur, trades, int(t[-1]), fill, bars_in_pos, reason="end")
        equity[-1] = cash

    rets = np.diff(equity, prepend=1.0) / np.maximum(1e-12, np.roll(equity, 1))
    rets[0] = equity[0] - 1.0
    bpy = infer_bars_per_year(t)
    years = max(1e-9, (t[-1] - t[0]) / (365.0 * 86400.0)) if n > 1 else 1e-9
    total = float(equity[-1] - 1.0)
    cagr = float((equity[-1]) ** (1.0 / years) - 1.0) if equity[-1] > 0 and years > 0.02 else total
    vol = float(np.std(rets))
    sharpe = float(np.mean(rets) / vol * math.sqrt(bpy)) if vol > 1e-12 else 0.0
    downside = rets[rets < 0]
    dvol = float(np.std(downside)) if len(downside) else 0.0
    sortino = float(np.mean(rets) / dvol * math.sqrt(bpy)) if dvol > 1e-12 else 0.0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.maximum(1e-12, peak)
    max_dd = float(np.min(dd)) if n else 0.0
    wins = [tr for tr in trades if tr["ret_pct"] > 0]
    losses = [tr for tr in trades if tr["ret_pct"] <= 0]
    gross_win = sum(tr["ret_pct"] for tr in wins)
    gross_loss = -sum(tr["ret_pct"] for tr in losses)
    exposure = sum(tr.get("bars", 0) for tr in trades) / max(1, n)
    bh = float(c[-1] / c[0] - 1.0) if n and c[0] > 0 else 0.0

    rets_all = [tr["ret_pct"] for tr in trades]
    return {
        "stats": {
            "total_return_pct": round(total * 100, 4),
            "buy_hold_return_pct": round(bh * 100, 4),
            "cagr_pct": round(cagr * 100, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "calmar": round(cagr / abs(max_dd), 4) if max_dd < -1e-9 else None,
            "max_drawdown_pct": round(max_dd * 100, 4),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 1e-12 else None,
            "expectancy_pct": round(float(np.mean(rets_all)), 4) if rets_all else None,
            "avg_win_pct": round(float(np.mean([t_["ret_pct"] for t_ in wins])), 4) if wins else None,
            "avg_loss_pct": round(float(np.mean([t_["ret_pct"] for t_ in losses])), 4) if losses else None,
            "best_trade_pct": round(max(rets_all), 4) if rets_all else None,
            "worst_trade_pct": round(min(rets_all), 4) if rets_all else None,
            "avg_bars_in_trade": round(float(np.mean([t_.get("bars", 0) for t_ in trades])), 1) if trades else None,
            "trades": len(trades),
            "long_trades": sum(1 for tr in trades if tr.get("side") != "short"),
            "short_trades": sum(1 for tr in trades if tr.get("side") == "short"),
            "exposure_pct": round(exposure * 100, 2),
            "leverage": lev if lev > 1.0 else None,
            "liquidated": liquidated or None,
            "bars": n,
            "years": round(years, 3),
            "engine": "native",
        },
        "equity_t": arr["t"].tolist(),
        "equity": [round(float(x), 6) for x in equity.tolist()],
        "trades": trades,
    }


def _close_trade(cur, trades, ts, fill, bars, reason="signal"):
    if not cur:
        return
    cur["exit_t"] = ts
    cur["exit_px"] = round(float(fill), 8)
    if not cur.get("entry_px"):
        cur["ret_pct"] = 0.0
    elif cur.get("side") == "short":
        # short P&L on notional: profit when price falls
        cur["ret_pct"] = round((cur["entry_px"] - fill) / cur["entry_px"] * 100.0, 4)
    else:
        cur["ret_pct"] = round((fill / cur["entry_px"] - 1.0) * 100.0, 4)
    cur["bars"] = bars
    cur["reason"] = reason
    cur["pnl"] = round(cur.get("notional", 0.0) * cur["ret_pct"] / 100.0, 2)
    trades.append(cur)


def crosses_above(a: "np.ndarray", b: "np.ndarray") -> "np.ndarray":
    prev_a, prev_b = np.roll(a, 1), np.roll(b, 1)
    out = (prev_a <= prev_b) & (a > b)
    out[0] = False
    return out & ~np.isnan(a) & ~np.isnan(b) & ~np.isnan(prev_a) & ~np.isnan(prev_b)


OPS = {
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "crosses_above": crosses_above,
    "crosses_below": lambda a, b: crosses_above(b, a),
}


def set_spec_path(spec: dict, path: str, value):
    """Set a value inside a (deep-copied) strategy spec by dotted path —
    list indices are numeric segments, e.g. 'entry.0.left.params.n'."""
    parts = [p for p in str(path).split(".") if p != ""]
    if not parts:
        raise ValueError("empty path")
    cur = spec
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        key = int(part) if part.lstrip("-").isdigit() and isinstance(cur, list) else part
        if last:
            cur[key] = value
        else:
            cur = cur[key]
    return spec


def sweep_axis_values(axis: dict) -> List[float]:
    """One sweep axis → concrete values: {values:[…]} or {from,to,step}."""
    if axis.get("values") is not None:
        vals = axis["values"]
        if isinstance(vals, str):
            vals = [v.strip() for v in vals.split(",") if v.strip()]
        return [float(v) for v in vals][:100]
    lo = float(axis.get("from", 0))
    hi = float(axis.get("to", lo))
    step = float(axis.get("step", 1))
    if step <= 0:
        raise ValueError("step must be > 0")
    out, v = [], lo
    while v <= hi + 1e-9 and len(out) < 200:
        # keep ints clean (period params) while allowing float axes
        out.append(round(v, 10))
        v += step
    return out


def sweep_grid(axes: List[dict], max_combos: int = 400) -> List[dict]:
    """Cartesian product of sweep axes → [{path:value,…}, …] (capped)."""
    if not axes:
        return []
    combos: List[dict] = [{}]
    for ax in axes[:3]:
        path = str(ax.get("path") or "")
        if not path:
            raise ValueError("sweep axis needs a path")
        vals = sweep_axis_values(ax)
        if not vals:
            raise ValueError(f"sweep axis '{path}' has no values")
        combos = [{**c, path: v} for c in combos for v in vals]
        if len(combos) > max_combos:
            raise ValueError(f"sweep too large: {len(combos)} combos (max {max_combos})")
    return combos


# metric -> higher-is-better; drawdown is stored negative so "max" works there too
SWEEP_METRICS = ("sharpe", "sortino", "total_return_pct", "cagr_pct", "calmar",
                 "profit_factor", "win_rate_pct", "expectancy_pct", "max_drawdown_pct")


def eval_conditions(arr, conds, resolve, mode="and") -> "np.ndarray":
    """conds: [{left, op, right}] — `resolve(operand)` returns an aligned array."""
    n = len(arr["c"])
    acc = None
    for cond in (conds or []):
        op = str(cond.get("op", ">"))
        if op not in OPS:
            raise ValueError(f"unknown op '{op}'")
        a = resolve(cond.get("left"))
        b = resolve(cond.get("right"))
        with np.errstate(invalid="ignore"):
            sig = OPS[op](a, b)
        sig = np.asarray(sig, dtype=bool) & ~np.isnan(a) & ~np.isnan(b)
        acc = sig if acc is None else ((acc & sig) if mode == "and" else (acc | sig))
    return acc if acc is not None else np.zeros(n, dtype=bool)


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE and HAS_NUMPY:

    def _md():
        return sys.modules.get("markets_data_capabilities")

    def _ma():
        return sys.modules.get("markets_analysis_capabilities")

    _TABLES_READY = False

    def _ensure_tables_sync():
        conn = _sqlite_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_ml_models (
                    id          TEXT PRIMARY KEY,
                    name        TEXT,
                    dataset_id  TEXT,
                    task        TEXT,
                    horizon     INTEGER DEFAULT 5,
                    features    TEXT,
                    model_kind  TEXT,
                    hyperparams TEXT,
                    metrics     TEXT,
                    status      TEXT,
                    blob        BLOB,
                    trained_at  TEXT,
                    created_at  TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_strategies (
                    id         TEXT PRIMARY KEY,
                    name       TEXT,
                    spec       TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )""")
            # v2 additions to mkt_strategies (kind rule|ml|fused, lifecycle, monitor)
            for _mig in (
                "ALTER TABLE mkt_strategies ADD COLUMN kind TEXT DEFAULT 'rule'",
                "ALTER TABLE mkt_strategies ADD COLUMN status TEXT DEFAULT 'draft'",
                "ALTER TABLE mkt_strategies ADD COLUMN members TEXT",
                "ALTER TABLE mkt_strategies ADD COLUMN monitor TEXT",
            ):
                try:
                    conn.execute(_mig)
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_alerts (
                    id          TEXT PRIMARY KEY,
                    kind        TEXT,
                    strategy_id TEXT,
                    name        TEXT,
                    dataset_id  TEXT,
                    direction   TEXT,
                    price       REAL,
                    message     TEXT,
                    seen        INTEGER DEFAULT 0,
                    created_at  TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_backtests (
                    id          TEXT PRIMARY KEY,
                    dataset_id  TEXT,
                    strategy_id TEXT,
                    name        TEXT,
                    spec        TEXT,
                    stats       TEXT,
                    result      TEXT,
                    created_at  TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_portfolio_tx (
                    id          TEXT PRIMARY KEY,
                    symbol_key  TEXT NOT NULL,
                    name        TEXT,
                    asset_class TEXT,
                    side        TEXT,
                    qty         REAL,
                    price       REAL,
                    fees        REAL DEFAULT 0,
                    currency    TEXT DEFAULT 'USD',
                    ts          TEXT,
                    note        TEXT,
                    created_at  TEXT
                )""")
            conn.commit()
        finally:
            conn.close()

    async def _ensure_tables():
        global _TABLES_READY
        if _TABLES_READY:
            return
        await asyncio.get_running_loop().run_in_executor(None, _ensure_tables_sync)
        _TABLES_READY = True

    # ── ML models ─────────────────────────────────────────────────────────────

    _ML_COLS = ("id", "name", "dataset_id", "task", "horizon", "features",
                "model_kind", "hyperparams", "metrics", "status", "trained_at", "created_at")

    def _ml_row_sync(mid: str, with_blob: bool = False):
        conn = _sqlite_conn()
        try:
            r = conn.execute("SELECT * FROM mkt_ml_models WHERE id=?", (mid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            if not with_blob:
                d.pop("blob", None)
            return d
        finally:
            conn.close()

    def _ml_public(d: dict) -> dict:
        out = {k: d.get(k) for k in _ML_COLS}
        for f in ("features", "hyperparams", "metrics"):
            try:
                out[f] = json.loads(out[f]) if out.get(f) else ({} if f != "features" else [])
            except Exception:
                out[f] = {} if f != "features" else []
        return out

    def _ml_update_sync(mid: str, **fields):
        conn = _sqlite_conn()
        try:
            sets, args = [], []
            for k, val in fields.items():
                sets.append(f"{k}=?"); args.append(val)
            args.append(mid)
            conn.execute(f"UPDATE mkt_ml_models SET {', '.join(sets)} WHERE id=?", args)
            conn.commit()
        finally:
            conn.close()

    def _make_model(task: str, kind: str, hp: dict):
        hp = dict(hp or {})
        if task == "classify":
            if kind == "gbt":
                return GradientBoostingClassifier(
                    n_estimators=int(hp.get("n_estimators", 200)),
                    max_depth=int(hp.get("max_depth", 3)),
                    learning_rate=float(hp.get("learning_rate", 0.05)))
            if kind == "rf":
                return RandomForestClassifier(
                    n_estimators=int(hp.get("n_estimators", 300)),
                    max_depth=int(hp.get("max_depth", 6)) or None,
                    min_samples_leaf=int(hp.get("min_samples_leaf", 5)), n_jobs=-1)
            return make_pipeline(StandardScaler(), LogisticRegression(
                C=float(hp.get("C", 1.0)), max_iter=int(hp.get("max_iter", 500))))
        # regress
        if kind == "gbt":
            return GradientBoostingRegressor(
                n_estimators=int(hp.get("n_estimators", 200)),
                max_depth=int(hp.get("max_depth", 3)),
                learning_rate=float(hp.get("learning_rate", 0.05)))
        if kind == "rf":
            return RandomForestRegressor(
                n_estimators=int(hp.get("n_estimators", 300)),
                max_depth=int(hp.get("max_depth", 6)) or None,
                min_samples_leaf=int(hp.get("min_samples_leaf", 5)), n_jobs=-1)
        return make_pipeline(StandardScaler(), Ridge(alpha=float(hp.get("alpha", 1.0))))

    def _train_sync(row: dict, arr: Dict[str, "np.ndarray"], compute) -> dict:
        """Blocking train + walk-forward eval. Returns {metrics, blob}."""
        feats = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
        hp = json.loads(row["hyperparams"] or "{}") if isinstance(row["hyperparams"], str) \
            else (row["hyperparams"] or {})
        task, kind, horizon = row["task"], row["model_kind"], max(1, int(row["horizon"]))
        X, names = build_features(arr, feats or DEFAULT_FEATURES, compute)
        c = arr["c"]
        fwd = np.full(len(c), np.nan)
        fwd[:-horizon] = c[horizon:] / c[:-horizon] - 1.0
        y = (fwd > 0).astype(int) if task == "classify" else fwd
        valid = ~np.isnan(fwd) & ~np.any(np.isnan(X), axis=1)
        Xv, yv, tv = X[valid], y[valid], arr["t"][valid]
        fwd_v = fwd[valid]
        if len(Xv) < 200:
            raise ValueError(f"only {len(Xv)} usable samples — need ≥200 (fetch more history)")

        n_splits = min(4, max(2, len(Xv) // 500))
        tss = TimeSeriesSplit(n_splits=n_splits)
        accs, f1s, maes, r2s, sig_rets = [], [], [], [], []
        for tr_idx, te_idx in tss.split(Xv):
            m = _make_model(task, kind, hp)
            m.fit(Xv[tr_idx], yv[tr_idx])
            pred = m.predict(Xv[te_idx])
            if task == "classify":
                accs.append(accuracy_score(yv[te_idx], pred))
                f1s.append(f1_score(yv[te_idx], pred, zero_division=0))
                sig_rets.extend((np.where(pred > 0, 1.0, 0.0) * fwd_v[te_idx]).tolist())
            else:
                maes.append(mean_absolute_error(yv[te_idx], pred))
                r2s.append(r2_score(yv[te_idx], pred))
                sig_rets.extend((np.where(pred > 0, 1.0, 0.0) * fwd_v[te_idx]).tolist())

        sig = np.asarray(sig_rets)
        bpy = infer_bars_per_year(arr["t"]) / horizon
        sig_sharpe = float(np.mean(sig) / np.std(sig) * math.sqrt(bpy)) \
            if len(sig) and np.std(sig) > 1e-12 else 0.0
        metrics = {
            "samples": int(len(Xv)), "features": names, "cv_folds": n_splits,
            "signal_sharpe": round(sig_sharpe, 4),
            "signal_total_return_pct": round(float(np.nansum(sig)) * 100, 3),
        }
        if task == "classify":
            base = float(np.mean(yv))
            metrics.update({
                "accuracy": round(float(np.mean(accs)), 4),
                "f1": round(float(np.mean(f1s)), 4),
                "baseline_up_rate": round(base, 4),
                "edge": round(float(np.mean(accs)) - max(base, 1 - base), 4),
            })
        else:
            metrics.update({"mae": round(float(np.mean(maes)), 6),
                            "r2": round(float(np.mean(r2s)), 4)})

        final = _make_model(task, kind, hp)
        final.fit(Xv, yv)
        buf = io.BytesIO()
        pickle.dump({"model": final, "features": feats or DEFAULT_FEATURES,
                     "names": names, "task": task, "horizon": horizon}, buf)
        return {"metrics": metrics, "blob": buf.getvalue()}

    async def _train_task(mid: str):
        md, ma = _md(), _ma()
        row = await asyncio.get_running_loop().run_in_executor(None, _ml_row_sync, mid)
        if not (row and md and ma):
            return
        try:
            await emit_event({"type": "markets.ml", "stage": "training", "id": mid,
                              "name": row.get("name")})
            bars = await md.get_bars(row["dataset_id"], 100_000)
            if not bars:
                raise ValueError(f"no bars for {row['dataset_id']}")
            arr = ma.bars_to_arrays(bars)
            res = await asyncio.get_running_loop().run_in_executor(
                None, _train_sync, row, arr, ma.compute_indicator)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _ml_update_sync(
                    mid, status="ready", metrics=json.dumps(res["metrics"]),
                    blob=res["blob"], trained_at=now_iso()))
            await emit_event({"type": "markets.ml", "stage": "trained", "id": mid,
                              "name": row.get("name"), "metrics": res["metrics"]})
        except Exception as e:
            log.warning("ml train %s: %s", mid, e)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _ml_update_sync(
                    mid, status="error",
                    metrics=json.dumps({"error": str(e)[:300]})))
            await emit_event({"type": "markets.ml", "stage": "error", "id": mid,
                              "error": str(e)[:300]})

    @capability(
        "markets.ml.create", http_method="POST", http_path="/markets/ml/create",
        http_tags=["markets"], memory="on",
        schema={"properties": {
            "task": {"enum": ML_TASKS},
            "model_kind": {"enum": MODEL_KINDS},
        }},
        description="Build a predictive ML tool over a stored bar series: choose features, "
                    "horizon and model, then training starts in the background (walk-forward "
                    "validated). Input: name (str!), dataset_id (str! e.g. "
                    "'mkt.binance.btc_usdt.1d'), task (classify=direction up/down | "
                    "regress=forward return), horizon (int=5 bars ahead), "
                    "features (list — from ret_1,ret_5,ret_10,rsi,macd_hist,stoch_k,bb_pctb,"
                    "atr_norm,vol_z,ema_ratio,roc,dow; default all), model_kind "
                    "(gbt|rf|logreg|ridge), hyperparams (object — e.g. {n_estimators,"
                    "max_depth,learning_rate}), train (bool=True). Output: {ok, id, status}.",
    )
    async def cap_ml_create(name: str = "", dataset_id: str = "", task: str = "classify",
                            horizon: int = 5, features=None, model_kind: str = "gbt",
                            hyperparams=None, train: bool = True, trace_id=None) -> dict:
        if not HAS_SKLEARN:
            return {"error": "scikit-learn not installed"}
        if not name.strip() or not dataset_id:
            return {"error": "name and dataset_id required"}
        task = task if task in ML_TASKS else "classify"
        model_kind = model_kind if model_kind in MODEL_KINDS else "gbt"
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
        await _ensure_tables()
        mid = uuid.uuid4().hex[:10]

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_ml_models (id,name,dataset_id,task,horizon,features,"
                    "model_kind,hyperparams,metrics,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, name.strip(), dataset_id, task, max(1, int(horizon)),
                     json.dumps(features or DEFAULT_FEATURES), model_kind,
                     json.dumps(hyperparams or {}), "{}",
                     "queued" if train else "draft", now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        if train:
            asyncio.create_task(_train_task(mid))
        return {"ok": True, "id": mid, "status": "queued" if train else "draft"}

    @capability(
        "markets.ml.list", http_method="GET", http_path="/markets/ml/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List ML predictive tools with status and validation metrics. "
                    "Output: {models:[{id,name,dataset_id,task,horizon,features,model_kind,"
                    "hyperparams,metrics,status,trained_at}]}.",
    )
    async def cap_ml_list(trace_id=None) -> dict:
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                rows = conn.execute(
                    "SELECT " + ",".join(_ML_COLS) +
                    " FROM mkt_ml_models ORDER BY created_at DESC").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        return {"models": [_ml_public(r) for r in rows], "count": len(rows)}

    @capability(
        "markets.ml.update", http_method="POST", http_path="/markets/ml/update",
        http_tags=["markets"], memory="on",
        description="Tweak an ML tool — change features, hyperparameters, horizon or name; "
                    "optionally retrain immediately. This is how Vera tunes a model. "
                    "Input: id (str!), features (list), hyperparams (object — merged over "
                    "existing), horizon (int), name (str), retrain (bool=True). "
                    "Output: {ok, id, status}.",
    )
    async def cap_ml_update(id: str = "", features=None, hyperparams=None,
                            horizon: int = 0, name: str = "", retrain: bool = True,
                            trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        row = await asyncio.get_running_loop().run_in_executor(None, _ml_row_sync, id)
        if not row:
            return {"error": "no such model", "id": id}
        fields = {}
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except Exception:
                features = [f.strip() for f in features.split(",") if f.strip()]
        if isinstance(features, list) and features:
            fields["features"] = json.dumps(features)
        if isinstance(hyperparams, str):
            try:
                hyperparams = json.loads(hyperparams)
            except Exception:
                hyperparams = None
        if isinstance(hyperparams, dict):
            cur = {}
            try:
                cur = json.loads(row.get("hyperparams") or "{}")
            except Exception:
                pass
            cur.update(hyperparams)
            fields["hyperparams"] = json.dumps(cur)
        if horizon and int(horizon) > 0:
            fields["horizon"] = int(horizon)
        if name.strip():
            fields["name"] = name.strip()
        if not fields and not retrain:
            return {"error": "nothing to update"}
        if fields:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _ml_update_sync(id, **fields))
        if retrain:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _ml_update_sync(id, status="queued"))
            asyncio.create_task(_train_task(id))
        return {"ok": True, "id": id, "status": "queued" if retrain else row.get("status")}

    @capability(
        "markets.ml.train", http_method="POST", http_path="/markets/ml/train",
        http_tags=["markets"], memory="on",
        description="(Re)train an ML tool in the background; progress arrives as "
                    "markets.ml events. Input: id (str!). Output: {ok, id, status}.",
    )
    async def cap_ml_train(id: str = "", trace_id=None) -> dict:
        if not HAS_SKLEARN:
            return {"error": "scikit-learn not installed"}
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        row = await asyncio.get_running_loop().run_in_executor(None, _ml_row_sync, id)
        if not row:
            return {"error": "no such model", "id": id}
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _ml_update_sync(id, status="queued"))
        asyncio.create_task(_train_task(id))
        return {"ok": True, "id": id, "status": "queued"}

    async def _ml_signal_series(mid: str, arr: Dict[str, "np.ndarray"]) -> "np.ndarray":
        """P(up) (or predicted return mapped to 0/1 via >0) for every bar — used
        by predictions and as an 'ml' operand in strategy conditions."""
        ma = _ma()
        row = await asyncio.get_running_loop().run_in_executor(
            None, _ml_row_sync, mid, True)
        if not row or not row.get("blob"):
            raise ValueError(f"model {mid} not trained")
        bundle = pickle.loads(row["blob"])
        X, _ = build_features(arr, bundle["features"], ma.compute_indicator)
        out = np.full(len(arr["c"]), np.nan)
        valid = ~np.any(np.isnan(X), axis=1)
        if not valid.any():
            return out
        model = bundle["model"]
        if bundle["task"] == "classify" and hasattr(model, "predict_proba"):
            out[valid] = model.predict_proba(X[valid])[:, 1]
        else:
            pred = model.predict(X[valid])
            out[valid] = pred
        return out

    @capability(
        "markets.ml.predict", http_method="POST", http_path="/markets/ml/predict",
        http_tags=["markets"], memory="on",
        description="Run a trained ML tool on the latest stored bars and return its "
                    "current prediction. Input: id (str!), dataset_id (str — override "
                    "the training dataset, e.g. same model on another timeframe). "
                    "Output: {id, prediction:{signal,direction,as_of,horizon_bars}, metrics}.",
    )
    async def cap_ml_predict(id: str = "", dataset_id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        md, ma = _md(), _ma()
        row = await asyncio.get_running_loop().run_in_executor(None, _ml_row_sync, id)
        if not row:
            return {"error": "no such model", "id": id}
        if row.get("status") != "ready":
            return {"error": f"model status is '{row.get('status')}' — train it first", "id": id}
        ds = dataset_id or row["dataset_id"]
        bars = await md.get_bars(ds, 2000)
        if len(bars) < 100:
            return {"error": f"not enough bars in {ds}"}
        arr = ma.bars_to_arrays(bars)
        try:
            sig = await _ml_signal_series(id, arr)
        except Exception as e:
            return {"error": str(e)[:300]}
        last = None
        for x in sig[::-1]:
            if not (isinstance(x, float) and math.isnan(x)):
                last = float(x)
                break
        if last is None:
            return {"error": "no valid prediction (features all-NaN)"}
        task = row["task"]
        direction = ("up" if last >= 0.5 else "down") if task == "classify" \
            else ("up" if last > 0 else "down")
        pred = {"signal": round(last, 4), "direction": direction,
                "task": task, "horizon_bars": row["horizon"],
                "as_of": int(arr["t"][-1]), "dataset_id": ds}
        metrics = {}
        try:
            metrics = json.loads(row.get("metrics") or "{}")
        except Exception:
            pass
        await emit_event({"type": "markets.ml", "stage": "predicted", "id": id,
                          "signal": pred["signal"], "direction": direction})
        return {"id": id, "name": row.get("name"), "prediction": pred, "metrics": metrics}

    @capability(
        "markets.ml.series", http_method="POST", http_path="/markets/ml/series",
        http_tags=["markets"], memory="off", silent=True,
        description="Full per-bar signal series of a trained ML tool (P(up) for "
                    "classifiers, predicted return for regressors) aligned to the "
                    "stored bars — lets the UI plot the model as a chart indicator "
                    "and the studio use it in visual strategy building. Input: "
                    "id (str!), dataset_id (str — override training dataset), "
                    "limit (int=3000). Output: {id, task, t:[unix_sec], "
                    "signal:[float|null]}.",
    )
    async def cap_ml_series(id: str = "", dataset_id: str = "", limit: int = 3000,
                            trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        md, ma = _md(), _ma()
        row = await asyncio.get_running_loop().run_in_executor(None, _ml_row_sync, id)
        if not row:
            return {"error": "no such model", "id": id}
        if row.get("status") != "ready":
            return {"error": f"model status is '{row.get('status')}' — train it first"}
        ds = dataset_id or row["dataset_id"]
        bars = await md.get_bars(ds, max(100, min(20_000, int(limit))))
        if len(bars) < 100:
            return {"error": f"not enough bars in {ds}"}
        arr = ma.bars_to_arrays(bars)
        try:
            sig = await _ml_signal_series(id, arr)
        except Exception as e:
            return {"error": str(e)[:300]}
        return {"id": id, "name": row.get("name"), "task": row.get("task"),
                "dataset_id": ds, "t": arr["t"].tolist(),
                "signal": [None if (isinstance(x, float) and math.isnan(x))
                           else round(float(x), 5) for x in sig.tolist()]}

    @capability(
        "markets.ml.delete", http_method="POST", http_path="/markets/ml/delete",
        http_tags=["markets"], memory="on",
        description="Delete an ML tool. Input: id (str!). Output: {ok}.",
    )
    async def cap_ml_delete(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_ml_models WHERE id=?", (id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        return {"ok": True, "id": id}

    # ── Strategies & backtesting ──────────────────────────────────────────────

    async def _resolve_operand_factory(arr, depth=0):
        """Returns an async-built sync resolver: operand -> aligned ndarray."""
        ma = _ma()
        cache: Dict[str, "np.ndarray"] = {}
        ml_cache: Dict[str, "np.ndarray"] = {}
        cx_cache: Dict[str, Dict[str, "np.ndarray"]] = {}
        ds_cache: Dict[str, "np.ndarray"] = {}
        strat_cache: Dict[str, "np.ndarray"] = {}

        async def _load_dataset_series(ds: str):
            """External series (funding, open interest, long/short ratio, WSB
            mentions, sentiment, macro…) forward-filled onto these bars — this
            is how market-dynamics/OSINT data participates in backtests."""
            if ds in ds_cache:
                return
            md = _md()
            bars = await md.get_bars(ds, 50_000)
            if len(bars) < 2:
                raise ValueError(f"dataset operand '{ds}' has no stored data")
            src = ma.bars_to_arrays(bars)
            t_bars = arr["t"]
            out = np.full(len(t_bars), np.nan)
            idx = np.searchsorted(src["t"], t_bars, side="right") - 1
            valid = idx >= 0
            out[valid] = src["c"][idx[valid]]
            ds_cache[ds] = out

        def _cx_series(ident: str) -> Dict[str, "np.ndarray"]:
            """Custom (cx_*) indicator: load its stored expressions and evaluate
            them against these bars — makes user-defined indicators usable as
            strategy operands ({kind:'cx_…'} or {custom:'…'})."""
            if ident not in cx_cache:
                getter = getattr(ma, "_custom_ind_get_sync", None)
                row = getter(ident) if getter else None
                if not row:
                    raise ValueError(f"custom indicator '{ident}' not found")
                cx_cache[ident] = ma.eval_custom_series(arr, row.get("series") or {})
            return cx_cache[ident]

        async def prload(operand):
            """Pre-load ML + external-dataset series (async) so the sync
            resolver can use them."""
            if isinstance(operand, dict) and operand.get("ml"):
                mid = str(operand["ml"])
                if mid not in ml_cache:
                    ml_cache[mid] = await _ml_signal_series(mid, arr)
            if isinstance(operand, dict) and operand.get("dataset"):
                await _load_dataset_series(str(operand["dataset"]))
            if isinstance(operand, str) and operand.strip().startswith("mkt."):
                await _load_dataset_series(operand.strip())
            if isinstance(operand, dict) and operand.get("strategy"):
                sid = str(operand["strategy"])
                if sid not in strat_cache:
                    if depth >= 2:
                        raise ValueError("strategy-operand chains nested too deep")
                    loop = asyncio.get_running_loop()
                    row = await loop.run_in_executor(None, _strategy_full_sync, sid)
                    if not row:
                        raise ValueError(f"strategy operand '{sid}' not found")
                    e_, x_, se_, sx_ = await _spec_signals(arr, row["spec"], depth + 1)
                    strat_cache[sid] = _position_series(e_, x_, se_, sx_)

        def resolve(operand):
            n = len(arr["c"])
            if operand is None:
                raise ValueError("condition operand missing")
            if isinstance(operand, (int, float)):
                return np.full(n, float(operand))
            if isinstance(operand, str):
                key = operand.strip().lower()
                if key in ("close", "open", "high", "low", "volume"):
                    return arr[{"close": "c", "open": "o", "high": "h",
                                "low": "l", "volume": "v"}[key]]
                if operand.strip().startswith("mkt."):
                    ds = operand.strip()
                    if ds not in ds_cache:
                        raise ValueError(f"dataset operand {ds} not preloaded")
                    return ds_cache[ds]
                try:
                    return np.full(n, float(key))
                except Exception:
                    operand = {"kind": key}
            if isinstance(operand, dict):
                if operand.get("strategy"):
                    sid = str(operand["strategy"])
                    if sid not in strat_cache:
                        raise ValueError(f"strategy operand {sid} not preloaded")
                    pos = strat_cache[sid]
                    leg = str(operand.get("leg") or "").lower()
                    if leg == "long":
                        return (pos > 0).astype(float)
                    if leg == "short":
                        return (pos < 0).astype(float)
                    if leg in ("flat", "out"):
                        return (pos == 0).astype(float)
                    return pos.astype(float)
                if operand.get("value") is not None:
                    return np.full(n, float(operand["value"]))
                if operand.get("dataset"):
                    ds = str(operand["dataset"])
                    if ds not in ds_cache:
                        raise ValueError(f"dataset operand {ds} not preloaded")
                    return ds_cache[ds]
                if operand.get("ml"):
                    mid = str(operand["ml"])
                    if mid not in ml_cache:
                        raise ValueError(f"ml operand {mid} not preloaded")
                    return ml_cache[mid]
                kind = str(operand.get("kind", "")).lower()
                if operand.get("custom") or kind.startswith("cx_"):
                    series = _cx_series(str(operand.get("custom") or kind))
                    name = operand.get("series") or next(iter(series.keys()))
                    if name not in series:
                        raise ValueError(f"custom indicator has no series '{name}' "
                                         f"(has {list(series.keys())})")
                    return series[name]
                params = operand.get("params") or {}
                ck = f"{kind}:{json.dumps(params, sort_keys=True)}"
                if ck not in cache:
                    cache[ck] = ma.compute_indicator(arr, kind, params)
                series = cache[ck]
                name = operand.get("series") or next(iter(series.keys()))
                if name not in series:
                    raise ValueError(f"indicator {kind} has no series '{name}' "
                                     f"(has {list(series.keys())})")
                return series[name]
            raise ValueError(f"cannot resolve operand {operand!r}")
        return resolve, prload

    def _strategy_full_sync(sid: str) -> Optional[dict]:
        """Strategy row with spec merged with its kind/members columns."""
        conn = _sqlite_conn()
        try:
            r = conn.execute("SELECT * FROM mkt_strategies WHERE id=?", (sid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            try:
                d["spec"] = json.loads(d.get("spec") or "{}")
            except Exception:
                d["spec"] = {}
            for f in ("members", "monitor"):
                try:
                    d[f] = json.loads(d[f]) if d.get(f) else None
                except Exception:
                    d[f] = None
            d["spec"]["kind"] = d.get("kind") or d["spec"].get("kind") or "rule"
            if d.get("members"):
                d["spec"]["members"] = d["members"]
            return d
        finally:
            conn.close()

    def _spec_kind(spec: dict) -> str:
        k = str(spec.get("kind") or "").lower()
        if k in ("rule", "ml", "fused", "regime", "dual"):
            return k
        if spec.get("regimes"):
            return "regime"
        if spec.get("long") or spec.get("short"):
            return "dual"
        if spec.get("ml_id"):
            return "ml"
        if spec.get("members"):
            return "fused"
        return "rule"

    def _position_series(entry, exit_, s_entry, s_exit):
        """Causal position track (+1 long / -1 short / 0 flat) at each bar from a
        strategy's signals — lets ANY strategy be used as a CONDITION operand
        ({strategy:id, leg:'long'|'short'}) inside another (conditional linking)."""
        n = len(entry)
        out = np.zeros(n)
        pos = 0
        for i in range(n):
            out[i] = pos
            if pos == 0:
                if entry[i]:
                    pos = 1
                elif s_entry is not None and s_entry[i]:
                    pos = -1
            elif pos == 1:
                if exit_[i]:
                    pos = 0
                if s_entry is not None and s_entry[i]:
                    pos = -1
            else:
                if s_exit is not None and s_exit[i]:
                    pos = 0
                if entry[i]:
                    pos = 1
        return out

    def _regime_masks(arr, src):
        """bull/bear/flat/any per-bar CAUSAL masks from a regime source
        (sma slope+side | supertrend | pivots). Shared by regime strategies
        and the generic regime_filter gate."""
        ma = _ma()
        c = arr["c"]; n = len(c); src = src or {}
        method = str(src.get("method") or "sma").lower()
        if method == "supertrend":
            stv = ma.compute_indicator(arr, "supertrend", {
                "n": int(src.get("n", 10)), "mult": float(src.get("mult", 3.0))})["st_dir"]
            ok = ~np.isnan(stv); bull = ok & (stv > 0); bear = ok & (stv < 0)
        elif method == "pivots":
            pvd = ma.compute_indicator(arr, "pivotdir", {
                "pct": float(src.get("pct", 5.0))})["pv_dir"]
            ok = ~np.isnan(pvd); bull = ok & (pvd > 0); bear = ok & (pvd < 0)
        else:
            nS = int(src.get("n", 200))
            sma = ma.compute_indicator(arr, "sma", {"n": nS})[f"sma{nS}"]
            slope = np.diff(sma, prepend=sma[0] if len(sma) else 0.0)
            ok = ~np.isnan(sma); bull = ok & (c > sma) & (slope > 0); bear = ok & (c < sma) & (slope < 0)
        flat = ok & ~bull & ~bear
        return {"bull": bull, "bear": bear, "flat": flat, "any": np.ones(n, dtype=bool)}

    async def _spec_signals(arr, spec, depth: int = 0):
        """Evaluate a spec, then apply its optional regime_filter gate — a
        directional veto that blocks entries/exits in named regimes (e.g.
        block_entry_in:['bear'], block_exit_in:['bull'] = 'don't buy in a bear,
        don't sell in a bull'). Works on top of ANY strategy kind."""
        entry, exit_, se, sx = await _spec_signals_core(arr, spec, depth)
        rf = spec.get("regime_filter") if isinstance(spec, dict) else None
        if rf:
            n = len(arr["c"])
            masks = _regime_masks(arr, rf.get("source") or {})

            def _blk(names):
                m = np.zeros(n, dtype=bool)
                for nm in (names or []):
                    mm = masks.get(str(nm).lower())
                    if mm is not None:
                        m = m | mm
                return m
            entry = entry & ~_blk(rf.get("block_entry_in"))
            exit_ = exit_ & ~_blk(rf.get("block_exit_in"))
            if se is not None:
                se = se & ~_blk(rf.get("block_short_entry_in"))
                if sx is not None:
                    sx = sx & ~_blk(rf.get("block_short_exit_in"))
        gates = {g: spec.get(g) for g in
                 ("entry_gate", "exit_gate", "short_entry_gate", "short_exit_gate")
                 if spec.get(g)} if isinstance(spec, dict) else {}
        if gates:
            # generalised conditional linking: entries/exits only fire while the
            # gate conditions hold (ANDed). Operands are the full DSL — price,
            # indicators, ML, external {dataset:…} series AND {strategy:id} (is
            # another strategy currently long/short) — so any strategy can be
            # conditioned on any signal, indicator or other strategy.
            gr, gp = await _resolve_operand_factory(arr, depth)
            for _lst in gates.values():
                for _c in _lst:
                    await gp(_c.get("left")); await gp(_c.get("right"))
            if "entry_gate" in gates:
                entry = entry & eval_conditions(arr, gates["entry_gate"], gr, "and")
            if "exit_gate" in gates:
                exit_ = exit_ & eval_conditions(arr, gates["exit_gate"], gr, "and")
            if se is not None and "short_entry_gate" in gates:
                se = se & eval_conditions(arr, gates["short_entry_gate"], gr, "and")
            if sx is not None and "short_exit_gate" in gates:
                sx = sx & eval_conditions(arr, gates["short_exit_gate"], gr, "and")
        return entry, exit_, se, sx

    async def _spec_signals_core(arr, spec: dict, depth: int = 0):
        """(entry, exit, short_entry, short_exit) boolean arrays for any
        strategy kind — the ONE evaluator shared by backtests and the live
        monitor, so what you test is exactly what gets monitored. The short
        arrays are None for long-only specs.
          rule  — {entry:[conds] AND, exit:[conds] OR,
                   short_entry:[conds] AND, short_exit:[conds] OR}
          ml    — {ml_id, enter_above:0.6, exit_below:0.45,
                   short_below:0.35, short_exit_above:0.55} on P(up)
          fused — {members:[{strategy_id}|inline spec…], combine:'all|any|majority',
                   exit_combine:'any|all'}"""
        n = len(arr["c"])
        kind = _spec_kind(spec)
        if kind == "regime":
            # Multi-stage / phased strategy: different member strategies arm in
            # different market regimes (bull/bear/flat/any). Regime masks come
            # from a per-bar CAUSAL classifier; member entries are gated by
            # their regime, exits stay ungated (a position must always be able
            # to close), and by default a regime CHANGE also exits — so a
            # bull-phase position doesn't linger into a bear phase.
            if depth >= 1:
                raise ValueError("regime strategies cannot be nested")
            regimes = spec.get("regimes") or []
            if not regimes:
                raise ValueError("regime strategy needs regimes:[{when, strategy_id|spec}]")
            ma = _ma()
            src = spec.get("regime_source") or {}
            method = str(src.get("method") or "sma").lower()
            c = arr["c"]
            if method == "supertrend":
                stv = ma.compute_indicator(
                    arr, "supertrend",
                    {"n": int(src.get("n", 10)), "mult": float(src.get("mult", 3.0))})["st_dir"]
                ok = ~np.isnan(stv)
                bull = ok & (stv > 0)
                bear = ok & (stv < 0)
            elif method == "pivots":
                pvd = ma.compute_indicator(
                    arr, "pivotdir", {"pct": float(src.get("pct", 5.0))})["pv_dir"]
                ok = ~np.isnan(pvd)
                bull = ok & (pvd > 0)
                bear = ok & (pvd < 0)
            else:                                # sma slope + side (default)
                nS = int(src.get("n", 200))
                sma = ma.compute_indicator(arr, "sma", {"n": nS})[f"sma{nS}"]
                slope = np.diff(sma, prepend=sma[0] if len(sma) else 0.0)
                ok = ~np.isnan(sma)
                bull = ok & (c > sma) & (slope > 0)
                bear = ok & (c < sma) & (slope < 0)
            flat = ok & ~bull & ~bear
            masks = {"bull": bull, "bear": bear, "flat": flat,
                     "any": np.ones(n, dtype=bool)}
            label = np.where(bull, 1, np.where(bear, -1, 0))
            changed = np.zeros(n, dtype=bool)
            changed[1:] = (label[1:] != label[:-1]) & ok[1:]
            loop = asyncio.get_running_loop()
            entry = np.zeros(n, dtype=bool)
            exit_ = np.zeros(n, dtype=bool)
            s_entry = s_exit = None
            for m in regimes[:4]:
                if not isinstance(m, dict):
                    raise ValueError("each regime needs {when, strategy_id|spec}")
                when = masks.get(str(m.get("when") or "any").lower())
                if when is None:
                    raise ValueError(f"unknown regime '{m.get('when')}' "
                                     "(bull|bear|flat|any)")
                sub = m.get("spec")
                if not sub and m.get("strategy_id"):
                    row = await loop.run_in_executor(
                        None, _strategy_full_sync, str(m["strategy_id"]))
                    if not row:
                        raise ValueError(f"regime member '{m['strategy_id']}' not found")
                    sub = row["spec"]
                if not isinstance(sub, dict):
                    raise ValueError("regime member needs strategy_id or inline spec")
                e, x, se, sx = await _spec_signals(arr, sub, depth + 1)
                entry |= (e & when)
                exit_ |= x
                if se is not None:
                    if s_entry is None:
                        s_entry = np.zeros(n, dtype=bool)
                        s_exit = np.zeros(n, dtype=bool)
                    s_entry |= (se & when)
                    s_exit |= (sx if sx is not None else np.zeros(n, dtype=bool))
            if spec.get("exit_on_regime_change", True):
                exit_ |= changed
                if s_exit is not None:
                    s_exit |= changed
            return entry, exit_, s_entry, s_exit
        if kind == "ml":
            mid = str(spec.get("ml_id") or spec.get("ml") or "")
            if not mid:
                raise ValueError("ml strategy needs ml_id")
            sig = await _ml_signal_series(mid, arr)
            ea = float(spec.get("enter_above", 0.6))
            xb = float(spec.get("exit_below", 0.45))
            ok = ~np.isnan(sig)
            s_entry = s_exit = None
            if spec.get("short_below") is not None:
                sb = float(spec.get("short_below"))
                sxa = float(spec.get("short_exit_above", min(0.99, sb + 0.2)))
                s_entry = ok & (sig < sb)
                s_exit = ok & (sig > sxa)
            return (ok & (sig > ea)), (ok & (sig < xb)), s_entry, s_exit
        if kind == "fused":
            if depth >= 1:
                raise ValueError("fused strategies cannot contain fused members")
            members = spec.get("members") or []
            if not members:
                raise ValueError("fused strategy needs members:[…]")
            entries, exits, s_entries, s_exits = [], [], [], []
            loop = asyncio.get_running_loop()
            for m in members[:6]:
                sub = None
                if isinstance(m, dict) and m.get("strategy_id"):
                    row = await loop.run_in_executor(
                        None, _strategy_full_sync, str(m["strategy_id"]))
                    if not row:
                        raise ValueError(f"fused member '{m['strategy_id']}' not found")
                    sub = row["spec"]
                elif isinstance(m, str):
                    row = await loop.run_in_executor(None, _strategy_full_sync, m)
                    if not row:
                        raise ValueError(f"fused member '{m}' not found")
                    sub = row["spec"]
                elif isinstance(m, dict):
                    sub = m
                if not isinstance(sub, dict):
                    raise ValueError("fused members must be strategy ids or inline specs")
                e, x, se, sx = await _spec_signals(arr, sub, depth + 1)
                entries.append(e); exits.append(x)
                if se is not None:
                    s_entries.append(se)
                    s_exits.append(sx if sx is not None else np.zeros(n, dtype=bool))
            E = np.column_stack(entries)
            mode = str(spec.get("combine") or "all").lower()
            weights = spec.get("weights")
            if weights or mode == "weighted":
                # WEIGHTED compound: each member votes with a weight; the
                # composite fires when the normalised weighted score crosses a
                # threshold. Weights + thresholds are ordinary numeric spec
                # leaves, so autotune/sweeps tune the composition itself.
                w = np.asarray([float(x) for x in (weights or [1.0] * E.shape[1])]
                               [:E.shape[1]], dtype=np.float64)
                if len(w) < E.shape[1]:
                    w = np.concatenate([w, np.ones(E.shape[1] - len(w))])
                wsum = float(np.abs(w).sum()) or 1.0
                ethr = float(spec.get("enter_threshold", 0.6))
                score = (E.astype(np.float64) @ w) / wsum
                entry = score >= ethr
                X = np.column_stack(exits)
                xthr = float(spec.get("exit_threshold", 0.34))
                xscore = (X.astype(np.float64) @ w) / wsum
                exit_ = xscore >= xthr
                s_entry = s_exit = None
                if s_entries:
                    SE = np.column_stack(s_entries)
                    ws = w[:SE.shape[1]] if len(w) >= SE.shape[1] else \
                        np.ones(SE.shape[1])
                    wssum = float(np.abs(ws).sum()) or 1.0
                    s_entry = (SE.astype(np.float64) @ ws) / wssum >= ethr
                    SX = np.column_stack(s_exits)
                    s_exit = (SX.astype(np.float64) @ ws) / wssum >= xthr
                return entry, exit_, s_entry, s_exit
            if mode == "any":
                entry = E.any(axis=1)
            elif mode == "majority":
                entry = E.sum(axis=1) * 2 > len(entries)
            else:
                entry = E.all(axis=1)
            X = np.column_stack(exits)
            exit_ = X.all(axis=1) if str(spec.get("exit_combine") or "any").lower() == "all" \
                else X.any(axis=1)
            s_entry = s_exit = None
            if s_entries:
                SE = np.column_stack(s_entries)
                s_entry = SE.all(axis=1) if mode == "all" else \
                    (SE.sum(axis=1) * 2 > len(s_entries)) if mode == "majority" else SE.any(axis=1)
                SX = np.column_stack(s_exits)
                s_exit = SX.all(axis=1) if str(spec.get("exit_combine") or "any").lower() == "all" \
                    else SX.any(axis=1)
            return entry, exit_, s_entry, s_exit
        if kind == "dual":
            # Compose one strategy on the LONG side with another on the SHORT
            # side into a single long/short strategy (each leg is a strategy id
            # or inline spec). If the short leg defines its own short side we
            # use that; otherwise its long signals drive the short entries.
            if depth >= 1:
                raise ValueError("dual strategies cannot be nested")
            loop = asyncio.get_running_loop()

            async def _leg(m):
                if not m:
                    return None
                sub = None
                if isinstance(m, dict) and m.get("spec"):
                    sub = m["spec"]
                elif isinstance(m, dict) and m.get("strategy_id"):
                    row = await loop.run_in_executor(None, _strategy_full_sync, str(m["strategy_id"]))
                    sub = row["spec"] if row else None
                elif isinstance(m, str):
                    row = await loop.run_in_executor(None, _strategy_full_sync, m)
                    sub = row["spec"] if row else None
                elif isinstance(m, dict):
                    sub = m
                if not isinstance(sub, dict):
                    raise ValueError("dual leg needs a strategy_id or inline spec")
                return await _spec_signals(arr, sub, depth + 1)
            L = await _leg(spec.get("long"))
            S = await _leg(spec.get("short"))
            if not L and not S:
                raise ValueError("dual strategy needs a long and/or short leg")
            z = np.zeros(n, dtype=bool)
            entry = L[0] if L else z
            exit_ = L[1] if L else z
            s_entry = s_exit = None
            if S:
                if S[2] is not None:
                    s_entry, s_exit = S[2], S[3]
                else:
                    s_entry, s_exit = S[0], S[1]
            return entry, exit_, s_entry, s_exit
        # rule
        if not spec.get("entry") and not spec.get("short_entry"):
            raise ValueError("rule strategy needs entry:[…] (and/or short_entry:[…]) conditions")
        resolve, prload = await _resolve_operand_factory(arr, depth)
        for cond in (list(spec.get("entry") or []) + list(spec.get("exit") or []) +
                     list(spec.get("short_entry") or []) + list(spec.get("short_exit") or [])):
            await prload(cond.get("left")); await prload(cond.get("right"))
        entry = eval_conditions(arr, spec.get("entry"), resolve, mode="and") \
            if spec.get("entry") else np.zeros(n, dtype=bool)
        exit_ = eval_conditions(arr, spec.get("exit"), resolve, mode="or") \
            if spec.get("exit") else np.zeros(n, dtype=bool)
        s_entry = s_exit = None
        if spec.get("short_entry"):
            s_entry = eval_conditions(arr, spec.get("short_entry"), resolve, mode="and")
            s_exit = eval_conditions(arr, spec.get("short_exit"), resolve, mode="or") \
                if spec.get("short_exit") else np.zeros(n, dtype=bool)
        return entry, exit_, s_entry, s_exit

    @capability(
        "markets.strategy.save", http_method="POST", http_path="/markets/strategy/save",
        http_tags=["markets"], memory="on",
        schema=enum_schema(kind=["rule", "ml", "fused", "regime", "dual"]),
        description="Save/update a trading strategy. Four kinds: "
                    "kind='rule' — spec {entry:[{left,op,right}…] ANDed, exit:[…] ORed, "
                    "short_entry:[…], short_exit:[…] (optional short side), "
                    "fee_bps, slippage_bps, size_pct, stop_loss_pct, take_profit_pct}; "
                    "operands 'close'|number|{kind:'ema',params:{n:50}}|{ml:'<model_id>'}"
                    "|{kind:'cx_<custom_indicator_id>',series:'<name>'}; "
                    "ops > < >= <= crosses_above crosses_below. "
                    "kind='ml' — spec {ml_id, enter_above:0.6, exit_below:0.45, "
                    "short_below, short_exit_above} trades a trained predictor directly. "
                    "kind='fused' — members (list of saved strategy ids and/or inline "
                    "specs) + spec {combine:'all|any|majority', exit_combine:'any|all'}. "
                    "kind='regime' — MULTI-STAGE: spec {regimes:[{when:'bull|bear|flat|any', "
                    "strategy_id|spec}…], regime_source:{method:'sma|supertrend|pivots', "
                    "n, mult, pct}, exit_on_regime_change:true} — different member "
                    "strategies arm in different market phases. "
                    "Input: name (str!), spec (object!), kind (str=rule), members (list — "
                    "for fused), id (str — update existing). Output: {ok, id, kind}.",
    )
    async def cap_strategy_save(name: str = "", spec=None, kind: str = "",
                                members=None, id: str = "", trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                return {"error": "spec must be a JSON object"}
        if not isinstance(spec, dict):
            spec = {}
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except Exception:
                members = [m.strip() for m in members.split(",") if m.strip()]
        if isinstance(members, list) and members:
            spec["members"] = members
        if kind:
            spec["kind"] = kind
        k = _spec_kind(spec)
        if k == "rule" and not spec.get("entry") and not spec.get("short_entry"):
            return {"error": "rule strategy needs spec.entry:[…] and/or spec.short_entry:[…]"}
        if k == "ml" and not (spec.get("ml_id") or spec.get("ml")):
            return {"error": "ml strategy needs spec.ml_id"}
        if k == "fused" and not spec.get("members"):
            return {"error": "fused strategy needs members:[…]"}
        if k == "regime" and not spec.get("regimes"):
            return {"error": "regime strategy needs spec.regimes:[{when, strategy_id|spec}]"}
        if k == "dual" and not (spec.get("long") or spec.get("short")):
            return {"error": "dual strategy needs spec.long and/or spec.short (strategy id or inline spec)"}
        await _ensure_tables()
        sid = id or uuid.uuid4().hex[:10]

        # Auto-version on overwrite: the previous spec is snapshotted BEFORE it
        # is replaced (KV 'stratver:<id>', newest first, capped) — an autotune
        # adopt / optimise re-run / manual edit can never lose a good setup.
        if id:
            loop = asyncio.get_running_loop()
            old = await loop.run_in_executor(None, _strategy_full_sync, id)
            if old and old.get("spec") and \
                    json.dumps(old["spec"], sort_keys=True) != json.dumps(spec, sort_keys=True):
                vkey = f"stratver:{id}"
                stack = await loop.run_in_executor(None, _setting_get_sync, vkey) or []
                if not isinstance(stack, list):
                    stack = []
                stack.insert(0, {"spec": old["spec"], "kind": old.get("kind"),
                                 "name": old.get("name"), "saved_at": now_iso()})
                await loop.run_in_executor(
                    None, lambda: _setting_set_sync(vkey, stack[:15]))

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO mkt_strategies "
                    "(id,name,spec,kind,status,members,monitor,created_at,updated_at) VALUES "
                    "(?,?,?,?,COALESCE((SELECT status FROM mkt_strategies WHERE id=?),'draft'),"
                    "?,(SELECT monitor FROM mkt_strategies WHERE id=?),"
                    "COALESCE((SELECT created_at FROM mkt_strategies WHERE id=?),?),?)",
                    (sid, name.strip(), json.dumps(spec), k, sid,
                     json.dumps(spec.get("members")) if spec.get("members") else None,
                     sid, sid, now_iso(), now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        return {"ok": True, "id": sid, "kind": k}

    @capability(
        "markets.strategy.list", http_method="GET", http_path="/markets/strategy/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List saved strategies. Output: {strategies:[{id,name,spec,updated_at}]}.",
    )
    async def cap_strategy_list(trace_id=None) -> dict:
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM mkt_strategies ORDER BY updated_at DESC").fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        for r in rows:
            for f, dflt in (("spec", {}), ("members", None), ("monitor", None)):
                try:
                    r[f] = json.loads(r[f]) if r.get(f) else dflt
                except Exception:
                    r[f] = dflt
            r["kind"] = r.get("kind") or _spec_kind(r.get("spec") or {})
            r["status"] = r.get("status") or "draft"
        return {"strategies": rows, "count": len(rows)}

    @capability(
        "markets.strategy.delete", http_method="POST", http_path="/markets/strategy/delete",
        http_tags=["markets"], memory="on",
        description="Delete a saved strategy. Input: id (str!). Output: {ok}.",
    )
    async def cap_strategy_delete(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_strategies WHERE id=?", (id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        return {"ok": True, "id": id}

    def _run_backtrader_sync(arr, entry, exit_, opts: dict) -> dict:
        """Run the same signal arrays through backtrader (independent engine +
        its analyzers). Returns the same result shape as run_backtest."""
        df = _pd.DataFrame({
            "open": arr["o"], "high": arr["h"], "low": arr["l"],
            "close": arr["c"], "volume": arr["v"],
        }, index=_pd.to_datetime(arr["t"], unit="s"))
        opts = opts or {}
        fee = float(opts.get("fee_bps", 10)) / 10_000.0
        sl = float(opts.get("stop_loss_pct", 0)) / 100.0
        tp = float(opts.get("take_profit_pct", 0)) / 100.0
        size_pct = max(1.0, min(99.0, float(opts.get("size_pct", 100)) * 0.98))
        n = len(df)
        equity: List[float] = []
        trades_out: List[dict] = []

        class _SigStrategy(_bt.Strategy):
            params = dict(entry=None, exit=None, sl=0.0, tp=0.0)

            def __init__(self):
                self._entry_px = None
                self._qty = 0.0
                self._exit_px = None

            def next(self):
                equity.append(float(self.broker.getvalue()))
                i = len(self.data) - 1
                if i >= n:
                    return
                if self.position:
                    px = float(self.data.close[0])
                    if self.p.sl > 0 and self._entry_px and px <= self._entry_px * (1 - self.p.sl):
                        self.close(); return
                    if self.p.tp > 0 and self._entry_px and px >= self._entry_px * (1 + self.p.tp):
                        self.close(); return
                    if bool(self.p.exit[i]):
                        self.close()
                else:
                    if bool(self.p.entry[i]):
                        self.buy()

            def notify_order(self, order):
                # trade.size is 0 by the time a trade closes — capture the
                # executed fill size/price here so per-trade returns are real.
                if order.status == order.Completed:
                    if order.isbuy():
                        self._entry_px = float(order.executed.price)
                        self._qty = abs(float(order.executed.size))
                    else:
                        self._exit_px = float(order.executed.price)

            def notify_trade(self, trade):
                if trade.isclosed:
                    try:
                        denom = max(1e-9, (self._entry_px or float(trade.price)) *
                                    max(self._qty, 1e-9))
                        trades_out.append({
                            "entry_t": int(_bt.num2date(trade.dtopen).timestamp()),
                            "exit_t": int(_bt.num2date(trade.dtclose).timestamp()),
                            "entry_px": round(float(self._entry_px or trade.price), 8),
                            "exit_px": round(float(self._exit_px), 8) if self._exit_px else None,
                            "ret_pct": round(float(trade.pnlcomm) / denom * 100, 4),
                            "pnl": round(float(trade.pnlcomm), 4),
                            "bars": int(trade.barlen or 0),
                            "reason": "signal",
                        })
                    except Exception:
                        pass

        cerebro = _bt.Cerebro(stdstats=False)
        cerebro.adddata(_bt.feeds.PandasData(dataname=df))
        cerebro.addstrategy(_SigStrategy, entry=entry, exit=exit_, sl=sl, tp=tp)
        cerebro.broker.setcash(100_000.0)
        cerebro.broker.setcommission(commission=fee)
        cerebro.addsizer(_bt.sizers.PercentSizer, percents=size_pct)
        cerebro.addanalyzer(_bt.analyzers.SQN, _name="sqn")
        cerebro.addanalyzer(_bt.analyzers.DrawDown, _name="dd")
        cerebro.addanalyzer(_bt.analyzers.TradeAnalyzer, _name="ta")
        strat = cerebro.run()[0]

        eq = np.asarray(equity, dtype=np.float64) / 100_000.0
        if len(eq) < n:                       # pad warmup bars at initial equity
            eq = np.concatenate([np.ones(n - len(eq)), eq])
        eq = eq[:n]
        rets = np.diff(eq, prepend=eq[0]) / np.maximum(1e-12, np.roll(eq, 1))
        rets[0] = 0.0
        bpy = infer_bars_per_year(arr["t"])
        years = max(1e-9, (arr["t"][-1] - arr["t"][0]) / (365.0 * 86400.0))
        total = float(eq[-1] - 1.0)
        cagr = float(eq[-1] ** (1.0 / years) - 1.0) if eq[-1] > 0 and years > 0.02 else total
        vol = float(np.std(rets))
        sharpe = float(np.mean(rets) / vol * math.sqrt(bpy)) if vol > 1e-12 else 0.0
        try:
            sqn = round(float(strat.analyzers.sqn.get_analysis().get("sqn") or 0), 3)
        except Exception:
            sqn = None
        try:
            max_dd = -float(strat.analyzers.dd.get_analysis().max.drawdown)
        except Exception:
            peak = np.maximum.accumulate(eq)
            max_dd = float(np.min((eq - peak) / np.maximum(1e-12, peak))) * 100.0
        wins = [t_ for t_ in trades_out if t_["ret_pct"] > 0]
        losses = [t_ for t_ in trades_out if t_["ret_pct"] <= 0]
        bh = float(arr["c"][-1] / arr["c"][0] - 1.0) if arr["c"][0] > 0 else 0.0
        return {
            "stats": {
                "total_return_pct": round(total * 100, 4),
                "buy_hold_return_pct": round(bh * 100, 4),
                "cagr_pct": round(cagr * 100, 4),
                "sharpe": round(sharpe, 4),
                "sqn": sqn,
                "max_drawdown_pct": round(max_dd, 4),
                "win_rate_pct": round(len(wins) / len(trades_out) * 100, 2) if trades_out else 0.0,
                "expectancy_pct": round(float(np.mean([t_["ret_pct"] for t_ in trades_out])), 4)
                    if trades_out else None,
                "avg_win_pct": round(float(np.mean([t_["ret_pct"] for t_ in wins])), 4) if wins else None,
                "avg_loss_pct": round(float(np.mean([t_["ret_pct"] for t_ in losses])), 4) if losses else None,
                "trades": len(trades_out),
                "bars": n, "years": round(years, 3),
                "engine": "backtrader",
            },
            "equity_t": arr["t"].tolist(),
            "equity": [round(float(x), 6) for x in eq.tolist()],
            "trades": trades_out,
        }

    @capability(
        "markets.backtest.engines", http_method="GET", http_path="/markets/backtest/engines",
        http_tags=["markets"], memory="off", silent=True,
        description="List available backtest engines. Output: {engines:[{id,available,notes}]}.",
    )
    async def cap_backtest_engines(trace_id=None) -> dict:
        return {"engines": [
            {"id": "native", "available": True,
             "notes": "vectorised bar simulator — next-open fills, intrabar SL/TP"},
            {"id": "backtrader", "available": bool(HAS_BACKTRADER and HAS_PANDAS),
             "notes": "backtrader Cerebro + SQN/DrawDown analyzers"
                      if HAS_BACKTRADER else "pip install backtrader"},
        ]}

    @capability(
        "markets.backtest.run", http_method="POST", http_path="/markets/backtest/run",
        http_tags=["markets"], memory="on",
        schema=enum_schema(engine=["native", "backtrader"]),
        description="Backtest any strategy kind (rule, ml, fused) over stored bars: "
                    "equity curve, trades, return vs buy&hold, CAGR, Sharpe, Sortino, "
                    "Calmar, max drawdown, win rate, profit factor, expectancy (+SQN on "
                    "the backtrader engine). Input: dataset_id (str!), strategy_id (str — "
                    "saved strategy) OR spec (object — inline, same schema as "
                    "markets.strategy.save incl. kind/ml_id/members), engine "
                    "(native|backtrader), start/end (ISO str), limit (int=20000 bars), "
                    "name (str — result label). Output: {ok, id, stats, equity_t, equity, "
                    "trades}.",
    )
    async def cap_backtest_run(dataset_id: str = "", strategy_id: str = "", spec=None,
                               engine: str = "native", start: str = "", end: str = "",
                               limit: int = 20000, name: str = "", save: bool = True,
                               trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        await _ensure_tables()
        md, ma = _md(), _ma()
        if not (md and ma):
            return {"error": "markets data/analysis modules unavailable"}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                return {"error": "spec must be a JSON object"}
        sname = name
        if not spec and strategy_id:
            row = await asyncio.get_running_loop().run_in_executor(
                None, _strategy_full_sync, strategy_id)
            if not row:
                return {"error": "no such strategy", "strategy_id": strategy_id}
            spec = row["spec"]
            sname = sname or row["name"]
        if not isinstance(spec, dict):
            return {"error": "strategy_id or spec required"}
        engine = (engine or "native").lower()
        if engine == "backtrader" and not (HAS_BACKTRADER and HAS_PANDAS):
            return {"error": "backtrader engine unavailable — pip install backtrader"}

        # id up-front so every progress event carries it — the UI's live run
        # feed correlates stages by this id.
        bid = uuid.uuid4().hex[:10]
        await emit_event({"type": "markets.backtest", "stage": "started", "id": bid,
                          "dataset_id": dataset_id, "name": sname or "ad-hoc",
                          "engine": engine})
        bars = await md.get_bars(dataset_id, max(200, min(100_000, int(limit))),
                                 md._iso_to_ms(start), md._iso_to_ms(end))
        if len(bars) < 50:
            await emit_event({"type": "markets.backtest", "stage": "error", "id": bid,
                              "error": f"only {len(bars)} bars stored"})
            return {"error": f"only {len(bars)} bars stored for {dataset_id} — fetch first"}
        arr = ma.bars_to_arrays(bars)
        await emit_event({"type": "markets.backtest", "stage": "bars", "id": bid,
                          "bars": int(len(arr["t"])),
                          "first": int(arr["t"][0]), "last": int(arr["t"][-1])})

        try:
            entry, exit_, s_entry, s_exit = await _spec_signals(arr, spec)
            await emit_event({"type": "markets.backtest", "stage": "signals", "id": bid,
                              "entry_count": int(entry.sum()),
                              "exit_count": int(exit_.sum()),
                              "short_entry_count": int(s_entry.sum()) if s_entry is not None else 0})
            t0 = time.time()
            await emit_event({"type": "markets.backtest", "stage": "simulating",
                              "id": bid, "engine": engine})
            if engine == "backtrader":
                if s_entry is not None:
                    return {"error": "the backtrader engine is long-only — run "
                                     "short/both strategies on the native engine"}
                res = await asyncio.get_running_loop().run_in_executor(
                    None, _run_backtrader_sync, arr, entry, exit_, spec)
            else:
                res = run_backtest(arr, entry, exit_, spec, s_entry, s_exit)
            elapsed = int((time.time() - t0) * 1000)
        except Exception as e:
            await emit_event({"type": "markets.backtest", "stage": "error", "id": bid,
                              "error": str(e)[:300]})
            return {"error": f"backtest failed: {e}"}

        # downsample the equity curve for transport
        eq_t, eq = res["equity_t"], res["equity"]
        if len(eq) > 600:
            step = len(eq) / 600.0
            idx = sorted({int(i * step) for i in range(600)} | {len(eq) - 1})
            eq_t = [eq_t[i] for i in idx]; eq = [eq[i] for i in idx]
        trades = res["trades"][-300:]

        record = {"id": bid, "dataset_id": dataset_id, "strategy_id": strategy_id or "",
                  "name": sname or "ad-hoc", "spec": spec, "stats": res["stats"],
                  "equity_t": eq_t, "equity": eq, "trades": trades,
                  "elapsed_ms": elapsed, "created_at": now_iso()}

        if save:
            def _ins():
                conn = _sqlite_conn()
                try:
                    conn.execute(
                        "INSERT INTO mkt_backtests (id,dataset_id,strategy_id,name,spec,stats,"
                        "result,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (bid, dataset_id, strategy_id or "", record["name"], json.dumps(spec),
                         json.dumps(res["stats"]),
                         json.dumps({"equity_t": eq_t, "equity": eq, "trades": trades}),
                         record["created_at"]))
                    conn.commit()
                finally:
                    conn.close()
            await asyncio.get_running_loop().run_in_executor(None, _ins)
            await emit_event({"type": "markets.backtest", "stage": "done", "id": bid,
                              "dataset_id": dataset_id, "name": record["name"],
                              "stats": res["stats"]})
        return {"ok": True, "saved": bool(save), **record}

    @capability(
        "markets.backtest.list", http_method="GET", http_path="/markets/backtest/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List stored backtest results (stats only). Input: dataset_id (str — "
                    "filter), limit (int=30). Output: {backtests:[{id,name,dataset_id,"
                    "strategy_id,stats,created_at}]}.",
    )
    async def cap_backtest_list(dataset_id: str = "", limit: int = 30, trace_id=None) -> dict:
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                if dataset_id:
                    rows = conn.execute(
                        "SELECT id,name,dataset_id,strategy_id,stats,created_at FROM "
                        "mkt_backtests WHERE dataset_id=? ORDER BY created_at DESC LIMIT ?",
                        (dataset_id, max(1, min(200, int(limit))))).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id,name,dataset_id,strategy_id,stats,created_at FROM "
                        "mkt_backtests ORDER BY created_at DESC LIMIT ?",
                        (max(1, min(200, int(limit))),)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        for r in rows:
            try:
                r["stats"] = json.loads(r["stats"]) if r.get("stats") else {}
            except Exception:
                r["stats"] = {}
        return {"backtests": rows, "count": len(rows)}

    @capability(
        "markets.backtest.get", http_method="GET", http_path="/markets/backtest/get",
        http_tags=["markets"], memory="off", silent=True,
        description="Full stored backtest result (equity curve + trades). Input: id (str!). "
                    "Output: {id,name,spec,stats,equity_t,equity,trades}.",
    )
    async def cap_backtest_get(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _get():
            conn = _sqlite_conn()
            try:
                r = conn.execute("SELECT * FROM mkt_backtests WHERE id=?", (id,)).fetchone()
                return dict(r) if r else None
            finally:
                conn.close()
        r = await asyncio.get_running_loop().run_in_executor(None, _get)
        if not r:
            return {"error": "no such backtest", "id": id}
        out = {"id": r["id"], "name": r["name"], "dataset_id": r["dataset_id"],
               "strategy_id": r["strategy_id"], "created_at": r["created_at"]}
        for src, dst in (("spec", "spec"), ("stats", "stats")):
            try:
                out[dst] = json.loads(r[src]) if r.get(src) else {}
            except Exception:
                out[dst] = {}
        try:
            out.update(json.loads(r["result"]) if r.get("result") else {})
        except Exception:
            pass
        return out

    @capability(
        "markets.backtest.delete", http_method="POST",
        http_path="/markets/backtest/delete", http_tags=["markets"], memory="on",
        description="Delete a stored backtest result (does not touch the strategy "
                    "it came from). Input: id (str!). Output: {ok, id}.",
    )
    async def cap_backtest_delete(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_backtests WHERE id=?", (id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        return {"ok": True, "id": id}

    # ── Signal preview & parameter sweep ──────────────────────────────────────

    async def _load_spec_and_arr(dataset_id: str, strategy_id: str, spec,
                                 start: str, end: str, limit: int):
        """Shared loader for run/signals/sweep: (spec, name, arr) or {'error':…}."""
        md, ma = _md(), _ma()
        if not (md and ma):
            return None, None, {"error": "markets data/analysis modules unavailable"}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                return None, None, {"error": "spec must be a JSON object"}
        sname = ""
        if not spec and strategy_id:
            row = await asyncio.get_running_loop().run_in_executor(
                None, _strategy_full_sync, strategy_id)
            if not row:
                return None, None, {"error": "no such strategy",
                                    "strategy_id": strategy_id}
            spec, sname = row["spec"], row["name"]
        if not isinstance(spec, dict):
            return None, None, {"error": "strategy_id or spec required"}
        bars = await md.get_bars(dataset_id, max(200, min(100_000, int(limit))),
                                 md._iso_to_ms(start), md._iso_to_ms(end))
        if len(bars) < 50:
            return None, None, {"error": f"only {len(bars)} bars stored for "
                                         f"{dataset_id} — fetch first"}
        return {"spec": spec, "name": sname}, ma.bars_to_arrays(bars), None

    @capability(
        "markets.backtest.signals", http_method="POST",
        http_path="/markets/backtest/signals", http_tags=["markets"],
        memory="off", silent=True,
        description="Evaluate a strategy's entry/exit signals over stored bars "
                    "WITHOUT running a backtest — validates the spec and returns "
                    "signal timestamps for chart preview. Input: dataset_id (str!), "
                    "strategy_id (str) OR spec (object — same schema as "
                    "markets.strategy.save), start/end (ISO str), limit (int=20000), "
                    "max_markers (int=400 — most recent kept). Output: {ok, bars, "
                    "entry_count, exit_count, entries:[ts…], exits:[ts…], "
                    "entry_now, exit_now, first_bar, last_bar}.",
    )
    async def cap_backtest_signals(dataset_id: str = "", strategy_id: str = "",
                                   spec=None, start: str = "", end: str = "",
                                   limit: int = 20000, max_markers: int = 400,
                                   trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        await _ensure_tables()
        loaded, arr, err = await _load_spec_and_arr(dataset_id, strategy_id, spec,
                                                    start, end, limit)
        if err:
            return err
        try:
            entry, exit_, s_entry, s_exit = await _spec_signals(arr, loaded["spec"])
        except Exception as e:
            return {"error": f"signal evaluation failed: {e}"}
        t = arr["t"]
        ent_ts = [int(t[i]) for i in np.where(entry)[0]]
        exi_ts = [int(t[i]) for i in np.where(exit_)[0]]
        mm = max(10, min(2000, int(max_markers)))
        out = {"ok": True, "dataset_id": dataset_id, "bars": int(len(t)),
               "entry_count": len(ent_ts), "exit_count": len(exi_ts),
               "entries": ent_ts[-mm:], "exits": exi_ts[-mm:],
               "entry_now": bool(entry[-1]), "exit_now": bool(exit_[-1]),
               "first_bar": int(t[0]), "last_bar": int(t[-1])}
        if s_entry is not None:
            se_ts = [int(t[i]) for i in np.where(s_entry)[0]]
            sx_ts = [int(t[i]) for i in np.where(s_exit)[0]]
            out.update({"short_entry_count": len(se_ts), "short_exit_count": len(sx_ts),
                        "short_entries": se_ts[-mm:], "short_exits": sx_ts[-mm:],
                        "short_entry_now": bool(s_entry[-1]),
                        "short_exit_now": bool(s_exit[-1])})
        return out

    # In-memory sweep registry — sweeps are throwaway explorations; the best
    # run is persisted as a normal backtest row.
    _SWEEPS: Dict[str, dict] = {}
    _SWEEP_KEYS = ("total_return_pct", "sharpe", "sortino", "max_drawdown_pct",
                   "win_rate_pct", "trades", "profit_factor", "cagr_pct",
                   "expectancy_pct", "calmar")

    async def _sweep_task(sid: str):
        st = _SWEEPS[sid]
        loop = asyncio.get_running_loop()
        try:
            spec, combos, metric = st["spec"], st["combos"], st["metric"]
            arr = st.pop("_arr")
            results: List[dict] = []
            step = max(1, len(combos) // 10)
            for i, combo in enumerate(combos):
                if st.get("cancel"):
                    st["status"] = "cancelled"
                    break
                s = json.loads(json.dumps(spec))
                try:
                    for path, val in combo.items():
                        v = int(val) if float(val).is_integer() else float(val)
                        set_spec_path(s, path, v)
                    entry, exit_, s_en, s_ex = await _spec_signals(arr, s)
                    res = await loop.run_in_executor(
                        None, run_backtest, arr, entry, exit_, s, s_en, s_ex)
                    stats = res["stats"]
                    results.append({"values": combo,
                                    "stats": {k: stats.get(k) for k in _SWEEP_KEYS}})
                except Exception as e:
                    results.append({"values": combo, "error": str(e)[:200]})
                st["done"] = i + 1
                if (i + 1) % step == 0 or i + 1 == len(combos):
                    await emit_event({"type": "markets.backtest", "stage": "sweep_progress",
                                      "sweep_id": sid, "done": i + 1,
                                      "total": len(combos)})
            valid = [r for r in results
                     if r.get("stats") and r["stats"].get(metric) is not None]
            valid.sort(key=lambda r: r["stats"][metric], reverse=True)
            errors = [r for r in results if r.get("error")]
            st["results"] = valid + errors
            st["best"] = valid[0] if valid else None

            # persist the winner as a regular backtest row so it shows in history
            if valid and st["status"] == "running":
                best_spec = json.loads(json.dumps(spec))
                for path, val in valid[0]["values"].items():
                    v = int(val) if float(val).is_integer() else float(val)
                    set_spec_path(best_spec, path, v)
                entry, exit_, s_en, s_ex = await _spec_signals(arr, best_spec)
                res = await loop.run_in_executor(
                    None, run_backtest, arr, entry, exit_, best_spec, s_en, s_ex)
                eq_t, eq = res["equity_t"], res["equity"]
                if len(eq) > 600:
                    fstep = len(eq) / 600.0
                    idx = sorted({int(i * fstep) for i in range(600)} | {len(eq) - 1})
                    eq_t = [eq_t[i] for i in idx]; eq = [eq[i] for i in idx]
                bid = uuid.uuid4().hex[:10]

                def _ins():
                    conn = _sqlite_conn()
                    try:
                        conn.execute(
                            "INSERT INTO mkt_backtests (id,dataset_id,strategy_id,name,"
                            "spec,stats,result,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (bid, st["dataset_id"], st.get("strategy_id") or "",
                             f"{st['name']} [sweep best]", json.dumps(best_spec),
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
            if st["status"] == "running":
                st["status"] = "done"
            await emit_event({"type": "markets.backtest", "stage": "sweep_done",
                              "sweep_id": sid, "status": st["status"],
                              "best": st.get("best"),
                              "best_backtest_id": st.get("best_backtest_id")})
        except Exception as e:
            log.warning("sweep %s failed: %s", sid, e)
            st["status"] = "error"
            st["error"] = str(e)[:300]
            await emit_event({"type": "markets.backtest", "stage": "sweep_error",
                              "sweep_id": sid, "error": st["error"]})

    @capability(
        "markets.backtest.sweep", http_method="POST",
        http_path="/markets/backtest/sweep", http_tags=["markets"], memory="on",
        description="Grid-search strategy parameters in the background (native "
                    "engine): every combination is backtested and ranked by a "
                    "metric; the winner is stored as a backtest result. Input: "
                    "dataset_id (str!), strategy_id (str) OR spec (object), params "
                    "(list! ≤3 axes: [{path:'entry.0.left.params.n', from:10, to:60, "
                    "step:10} or {path, values:[…]}] — dotted paths into the spec, "
                    "list indices numeric), metric (sharpe|sortino|total_return_pct|"
                    "cagr_pct|calmar|profit_factor|win_rate_pct|expectancy_pct|"
                    "max_drawdown_pct = sharpe), limit (int=20000 bars), name (str). "
                    "≤400 combos. Progress via markets.backtest events (stage "
                    "sweep_progress/sweep_done); poll markets.backtest.sweep_status. "
                    "Output: {ok, sweep_id, combos, metric}.",
    )
    async def cap_backtest_sweep(dataset_id: str = "", strategy_id: str = "",
                                 spec=None, params=None, metric: str = "sharpe",
                                 limit: int = 20000, name: str = "",
                                 trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        await _ensure_tables()
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                return {"error": "params must be a JSON list of sweep axes"}
        if not isinstance(params, list) or not params:
            return {"error": "params required — [{path, from, to, step} | "
                             "{path, values:[…]}]"}
        metric = (metric or "sharpe").lower()
        if metric not in SWEEP_METRICS:
            return {"error": f"unknown metric '{metric}'",
                    "valid": list(SWEEP_METRICS)}
        loaded, arr, err = await _load_spec_and_arr(dataset_id, strategy_id, spec,
                                                    "", "", limit)
        if err:
            return err
        try:
            combos = sweep_grid(params)
        except Exception as e:
            return {"error": str(e)}
        # validate paths against the spec before launching
        probe = json.loads(json.dumps(loaded["spec"]))
        try:
            for path in {p for c in combos[:1] for p in c}:
                set_spec_path(probe, path, 1)
        except Exception as e:
            return {"error": f"bad sweep path: {e}"}

        sid = uuid.uuid4().hex[:10]
        _SWEEPS[sid] = {
            "id": sid, "status": "running", "dataset_id": dataset_id,
            "strategy_id": strategy_id or "", "name": name or loaded["name"] or "sweep",
            "metric": metric, "spec": loaded["spec"], "combos": combos,
            "total": len(combos), "done": 0, "results": [], "best": None,
            "created_at": now_iso(), "_arr": arr,
        }
        if len(_SWEEPS) > 20:                    # keep the registry bounded
            for old in sorted(_SWEEPS.values(), key=lambda s: s["created_at"])[:-20]:
                if old["status"] != "running":
                    _SWEEPS.pop(old["id"], None)
        asyncio.create_task(_sweep_task(sid))
        await emit_event({"type": "markets.backtest", "stage": "sweep_start",
                          "sweep_id": sid, "combos": len(combos),
                          "dataset_id": dataset_id, "metric": metric})
        return {"ok": True, "sweep_id": sid, "combos": len(combos),
                "metric": metric, "dataset_id": dataset_id}

    @capability(
        "markets.backtest.sweep_status", http_method="GET",
        http_path="/markets/backtest/sweep/status", http_tags=["markets"],
        memory="off", silent=True,
        description="Status/results of a parameter sweep. Input: id (str — omit to "
                    "list recent sweeps), top (int=40 — best rows returned), "
                    "cancel (bool — stop a running sweep). Output: {sweep:{id,status,"
                    "done,total,metric,best,best_backtest_id,best_spec,results:[…]}} "
                    "or {sweeps:[…]}.",
    )
    async def cap_backtest_sweep_status(id: str = "", top: int = 40,
                                        cancel: bool = False, trace_id=None) -> dict:
        if not id:
            return {"sweeps": [{k: s.get(k) for k in
                                ("id", "status", "done", "total", "metric",
                                 "dataset_id", "name", "created_at")}
                               for s in sorted(_SWEEPS.values(),
                                               key=lambda s: s["created_at"],
                                               reverse=True)]}
        st = _SWEEPS.get(id)
        if not st:
            return {"error": "no such sweep (results are kept in memory until "
                             "restart)", "id": id}
        if cancel and st["status"] == "running":
            st["cancel"] = True
        out = {k: st.get(k) for k in
               ("id", "status", "done", "total", "metric", "dataset_id", "name",
                "best", "best_backtest_id", "best_spec", "error", "created_at")}
        out["results"] = (st.get("results") or [])[:max(1, min(200, int(top)))]
        return {"sweep": out}

    # ── Strategy acceptance, live monitoring & alerts ─────────────────────────

    def _setting_get_sync(key: str) -> Optional[dict]:
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

    def _setting_set_sync(key: str, value: dict):
        conn = _sqlite_conn()
        try:
            conn.execute("INSERT OR REPLACE INTO mkt_settings (key, value, updated_at) "
                         "VALUES (?,?,?)", (key, json.dumps(value), now_iso()))
            conn.commit()
        finally:
            conn.close()

    _ALERT_ICON = {"entry": "🟢▲", "exit": "🔵▽", "short_entry": "🔴▼", "short_exit": "🟡△"}
    _ALERT_ACT = {"entry": "BUY (open long)", "exit": "SELL (close long)",
                  "short_entry": "SHORT (open)", "short_exit": "COVER (close short)"}

    async def _raise_alert(strategy: dict, direction: str, dataset_id: str,
                           price: float, channels: List[str], extra: dict = None):
        extra = extra or {}
        aid = uuid.uuid4().hex[:10]
        bits = [f"{_ALERT_ICON.get(direction, '•')} {strategy.get('name')} — "
                f"{_ALERT_ACT.get(direction, direction.upper())}",
                f"{str(dataset_id).replace('mkt.', '')} @ {price}"]
        pnl = extra.get("pnl_pct")
        if pnl is not None:
            bits.append(f"{'🟩' if pnl >= 0 else '🟥'} P&L {'+' if pnl >= 0 else ''}{round(pnl, 2)}%")
        if extra.get("account"):
            bits.append(f"[{extra.get('mode', 'sim')}: {extra['account']}]")
        msg = " · ".join(bits)

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_alerts (id,kind,strategy_id,name,dataset_id,direction,"
                    "price,message,seen,created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
                    (aid, "signal", strategy.get("id"), strategy.get("name"),
                     dataset_id, direction, float(price), msg, now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.alert", "id": aid, "kind": "signal",
                          "strategy_id": strategy.get("id"), "name": strategy.get("name"),
                          "dataset_id": dataset_id, "direction": direction,
                          "price": price, "pnl_pct": pnl,
                          "mode": extra.get("mode"), "account": extra.get("account"),
                          "message": msg})
        if "telegram" in (channels or []):
            tg = sys.modules.get("telegram_capabilities")
            if tg and hasattr(tg, "tg_notify"):
                try:
                    await tg.tg_notify(text="📈 " + msg)
                except Exception as e:
                    log.debug("alert telegram: %s", e)
        return aid

    async def _monitor_strategy(row: dict):
        """One monitor pass for an accepted strategy: recompute signals on the
        latest bars and alert on a NEW entry/exit signal at the newest bar."""
        md, ma = _md(), _ma()
        mon = row.get("monitor") or {}
        ds = mon.get("dataset_id") or ""
        if not (md and ma and ds):
            return
        state_key = f"mon:{row['id']}"
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(None, _setting_get_sync, state_key) or {}
        bars = await md.get_bars(ds, 800)
        if len(bars) < 60:
            state.update({"last_check": now_iso(), "error": f"only {len(bars)} bars in {ds}"})
            await loop.run_in_executor(None, lambda: _setting_set_sync(state_key, state))
            return
        arr = ma.bars_to_arrays(bars)
        try:
            entry, exit_, s_entry, s_exit = await _spec_signals(arr, row["spec"])
        except Exception as e:
            state.update({"last_check": now_iso(), "error": str(e)[:200]})
            await loop.run_in_executor(None, lambda: _setting_set_sync(state_key, state))
            return
        ts_last = int(arr["t"][-1])
        px_last = float(arr["c"][-1])
        pos = state.get("position", "flat")
        channels = mon.get("channels") or ["event"]

        async def _sim_trade(side: str):
            """Auto-trade the signal into a linked sim account (paper trading)."""
            sim = mon.get("sim_account_id")
            if not sim:
                return
            studio = sys.modules.get("markets_studio_capabilities")
            if not (studio and hasattr(studio, "sim_execute_signal")):
                return
            parts = ds.split(".")
            sym_key = f"{parts[1]}:{parts[2]}" if len(parts) >= 4 else ds
            await studio.sim_execute_signal(
                sim, sym_key, side, float(mon.get("sim_pct") or 25.0),
                px_last, f"strategy:{row.get('id')}")

        acct = mon.get("sim_account_id")
        exa = {"account": acct, "mode": "sim"} if acct else {}
        if pos != "long" and bool(entry[-1]) and state.get("last_entry_ts") != ts_last:
            await _raise_alert(row, "entry", ds, px_last, channels, exa)
            await _sim_trade("buy")
            state.update({"position": "long", "last_entry_ts": ts_last, "entry_px": px_last,
                          "last_signal": "entry", "last_signal_at": now_iso()})
        elif pos == "long" and bool(exit_[-1]) and state.get("last_exit_ts") != ts_last:
            ep = state.get("entry_px")
            pnl = ((px_last / ep - 1) * 100) if ep else None
            await _raise_alert(row, "exit", ds, px_last, channels, dict(exa, pnl_pct=pnl))
            await _sim_trade("sell")
            state.update({"position": "flat", "last_exit_ts": ts_last,
                          "last_signal": "exit", "last_signal_at": now_iso()})
        elif s_entry is not None and pos == "flat" and bool(s_entry[-1]) and \
                state.get("last_short_ts") != ts_last:
            await _raise_alert(row, "short_entry", ds, px_last, channels, exa)
            state.update({"position": "short", "last_short_ts": ts_last, "short_px": px_last,
                          "last_signal": "short_entry", "last_signal_at": now_iso()})
        elif s_exit is not None and pos == "short" and bool(s_exit[-1]) and \
                state.get("last_short_exit_ts") != ts_last:
            sp = state.get("short_px")
            pnl = ((sp / px_last - 1) * 100) if sp else None
            await _raise_alert(row, "short_exit", ds, px_last, channels, dict(exa, pnl_pct=pnl))
            state.update({"position": "flat", "last_short_exit_ts": ts_last,
                          "last_signal": "short_exit", "last_signal_at": now_iso()})
        state["last_check"] = now_iso()
        state["last_bar"] = ts_last
        state.pop("error", None)
        await loop.run_in_executor(None, lambda: _setting_set_sync(state_key, state))

    async def _monitor_tick():
        """Every minute: run due monitors for accepted strategies."""
        try:
            await _ensure_tables()
            loop = asyncio.get_running_loop()

            def _accepted():
                conn = _sqlite_conn()
                try:
                    rows = conn.execute(
                        "SELECT id FROM mkt_strategies WHERE status='accepted'").fetchall()
                    return [r[0] for r in rows]
                finally:
                    conn.close()
            for sid in await loop.run_in_executor(None, _accepted):
                row = await loop.run_in_executor(None, _strategy_full_sync, sid)
                if not row:
                    continue
                mon = row.get("monitor") or {}
                if not mon.get("enabled"):
                    continue
                state = await loop.run_in_executor(None, _setting_get_sync, f"mon:{sid}") or {}
                try:
                    from datetime import datetime as _dt
                    last = _dt.fromisoformat(str(state.get("last_check", "1970-01-01")).replace("Z", ""))
                    due = (time.time() - last.timestamp()) >= max(1, int(mon.get("interval_min", 15))) * 60
                except Exception:
                    due = True
                if due:
                    await _monitor_strategy(row)
        except Exception as e:
            log.debug("monitor tick: %s", e)

    schedule(_monitor_tick, 60, "mkt_strategy_monitor")

    @capability(
        "markets.strategy.accept", http_method="POST", http_path="/markets/strategy/accept",
        http_tags=["markets"], memory="on",
        description="Accept a strategy for LIVE MONITORING: its signals are re-evaluated "
                    "on fresh bars on a schedule and every new entry/exit signal raises an "
                    "alert (markets.alert event; optional Telegram via tg.notify). "
                    "Input: id (str! — saved strategy), dataset_id (str! — bars to monitor, "
                    "e.g. 'mkt.binance.btc_usdt.1h'), interval_min (int=15), channels "
                    "(list — 'event','telegram'; default ['event']), enabled (bool=True), "
                    "sim_account_id (str — link a markets.sim account so every signal is "
                    "auto-traded on paper), sim_pct (float=25 — % of sim cash per entry). "
                    "Output: {ok, id, monitor}.",
    )
    async def cap_strategy_accept(id: str = "", dataset_id: str = "",
                                  interval_min: int = 15, channels=None,
                                  enabled: bool = True, sim_account_id: str = "",
                                  sim_pct: float = 25.0, trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        row = await asyncio.get_running_loop().run_in_executor(None, _strategy_full_sync, id)
        if not row:
            return {"error": "no such strategy", "id": id}
        ds = dataset_id or (row.get("monitor") or {}).get("dataset_id") or ""
        if not ds and enabled:
            return {"error": "dataset_id required (which bars should be monitored?)"}
        if isinstance(channels, str):
            channels = [c.strip() for c in channels.split(",") if c.strip()]
        mon = {"enabled": bool(enabled), "dataset_id": ds,
               "interval_min": max(1, int(interval_min)),
               "channels": channels or ["event"], "accepted_at": now_iso()}
        prev_mon = row.get("monitor") or {}
        if sim_account_id:
            mon["sim_account_id"] = sim_account_id
            mon["sim_pct"] = max(1.0, min(100.0, float(sim_pct or 25.0)))
        elif prev_mon.get("sim_account_id"):
            mon["sim_account_id"] = prev_mon["sim_account_id"]
            mon["sim_pct"] = prev_mon.get("sim_pct", 25.0)

        def _upd():
            conn = _sqlite_conn()
            try:
                conn.execute("UPDATE mkt_strategies SET status='accepted', monitor=?, "
                             "updated_at=? WHERE id=?", (json.dumps(mon), now_iso(), id))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _upd)
        await emit_event({"type": "markets.monitor", "stage": "accepted", "id": id,
                          "name": row.get("name"), "dataset_id": ds})
        if enabled:
            asyncio.create_task(_monitor_strategy({**row, "monitor": mon}))
        return {"ok": True, "id": id, "monitor": mon}

    @capability(
        "markets.strategy.archive", http_method="POST", http_path="/markets/strategy/archive",
        http_tags=["markets"], memory="on",
        description="Stop monitoring a strategy and archive it (keeps the spec + results). "
                    "Input: id (str!). Output: {ok}.",
    )
    async def cap_strategy_archive(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _upd():
            conn = _sqlite_conn()
            try:
                r = conn.execute("SELECT monitor FROM mkt_strategies WHERE id=?",
                                 (id,)).fetchone()
                mon = {}
                try:
                    mon = json.loads(r[0]) if r and r[0] else {}
                except Exception:
                    pass
                mon["enabled"] = False
                conn.execute("UPDATE mkt_strategies SET status='archived', monitor=?, "
                             "updated_at=? WHERE id=?", (json.dumps(mon), now_iso(), id))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _upd)
        await emit_event({"type": "markets.monitor", "stage": "archived", "id": id})
        return {"ok": True, "id": id}

    @capability(
        "markets.monitor.status", http_method="GET", http_path="/markets/monitor/status",
        http_tags=["markets"], memory="off", silent=True,
        description="Live-monitoring dashboard: every accepted strategy with its monitor "
                    "config, virtual position, last check/signal and recent alerts. "
                    "Output: {monitors:[{id,name,kind,dataset_id,interval_min,channels,"
                    "state:{position,last_check,last_signal,...}}], alerts_unseen}.",
    )
    async def cap_monitor_status(trace_id=None) -> dict:
        await _ensure_tables()
        loop = asyncio.get_running_loop()

        def _rows():
            conn = _sqlite_conn()
            try:
                rows = conn.execute("SELECT id FROM mkt_strategies "
                                    "WHERE status='accepted'").fetchall()
                unseen = conn.execute("SELECT COUNT(*) FROM mkt_alerts WHERE seen=0").fetchone()
                return [r[0] for r in rows], int(unseen[0] if unseen else 0)
            finally:
                conn.close()
        ids, unseen = await loop.run_in_executor(None, _rows)
        out = []
        for sid in ids:
            row = await loop.run_in_executor(None, _strategy_full_sync, sid)
            if not row:
                continue
            mon = row.get("monitor") or {}
            state = await loop.run_in_executor(None, _setting_get_sync, f"mon:{sid}") or {}
            out.append({"id": sid, "name": row.get("name"), "kind": row.get("kind"),
                        "dataset_id": mon.get("dataset_id"),
                        "interval_min": mon.get("interval_min"),
                        "channels": mon.get("channels"), "enabled": mon.get("enabled"),
                        "sim_account_id": mon.get("sim_account_id"),
                        "sim_pct": mon.get("sim_pct"),
                        "state": state})
        return {"monitors": out, "count": len(out), "alerts_unseen": unseen}

    @capability(
        "markets.alerts.list", http_method="GET", http_path="/markets/alerts",
        http_tags=["markets"], memory="off", silent=True,
        description="List strategy signal alerts (newest first). Input: limit (int=50), "
                    "unseen_only (bool=False). Output: {alerts:[{id,strategy_id,name,"
                    "dataset_id,direction,price,message,seen,created_at}], unseen}.",
    )
    async def cap_alerts_list(limit: int = 50, unseen_only: bool = False, trace_id=None) -> dict:
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                q = "SELECT * FROM mkt_alerts"
                if unseen_only:
                    q += " WHERE seen=0"
                q += " ORDER BY created_at DESC LIMIT ?"
                rows = conn.execute(q, (max(1, min(500, int(limit))),)).fetchall()
                unseen = conn.execute("SELECT COUNT(*) FROM mkt_alerts WHERE seen=0").fetchone()
                return [dict(r) for r in rows], int(unseen[0] if unseen else 0)
            finally:
                conn.close()
        rows, unseen = await asyncio.get_running_loop().run_in_executor(None, _rows)
        return {"alerts": rows, "count": len(rows), "unseen": unseen}

    @capability(
        "markets.alerts.ack", http_method="POST", http_path="/markets/alerts/ack",
        http_tags=["markets"], memory="off",
        description="Mark alert(s) as seen. Input: id (str — one alert; blank = all). "
                    "Output: {ok, acked}.",
    )
    async def cap_alerts_ack(id: str = "", trace_id=None) -> dict:
        await _ensure_tables()

        def _upd():
            conn = _sqlite_conn()
            try:
                if id:
                    cur = conn.execute("UPDATE mkt_alerts SET seen=1 WHERE id=?", (id,))
                else:
                    cur = conn.execute("UPDATE mkt_alerts SET seen=1 WHERE seen=0")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        n = await asyncio.get_running_loop().run_in_executor(None, _upd)
        return {"ok": True, "acked": n}

    # ── Portfolio ─────────────────────────────────────────────────────────────

    @capability(
        "markets.portfolio.tx_add", http_method="POST", http_path="/markets/portfolio/tx_add",
        http_tags=["markets"], memory="on",
        schema=enum_schema(side=["buy", "sell"]),
        description="Record a portfolio transaction (works for crypto, stocks, collectables, "
                    "cards — any tracked or custom asset). Input: symbol_key (str! — "
                    "'provider:symbol', e.g. 'binance:BTC/USDT', 'yahoo:AAPL', "
                    "'custom:Charizard 1st Ed'), side (buy|sell), qty (float!), "
                    "price (float! — unit price), fees (float=0), ts (ISO str — default now), "
                    "name (str — display name), asset_class (str — crypto|stock|etf|"
                    "collectable|video_game|trading_card|other), note (str). "
                    "Output: {ok, id}.",
    )
    async def cap_portfolio_tx_add(symbol_key: str = "", side: str = "buy",
                                   qty: float = 0.0, price: float = 0.0,
                                   fees: float = 0.0, ts: str = "", name: str = "",
                                   asset_class: str = "", note: str = "",
                                   trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        side = side if side in ("buy", "sell") else "buy"
        try:
            qty, price, fees = float(qty), float(price), float(fees or 0)
        except Exception:
            return {"error": "qty/price must be numbers"}
        if qty <= 0 or price < 0:
            return {"error": "qty must be > 0"}
        await _ensure_tables()
        tid = uuid.uuid4().hex[:10]
        when = ts or now_iso()

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_portfolio_tx (id,symbol_key,name,asset_class,side,qty,"
                    "price,fees,ts,note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, symbol_key, name or symbol_key.split(":", 1)[-1],
                     asset_class or "", side, qty, price, fees, when, note or "", now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.portfolio", "stage": "tx_added",
                          "symbol_key": symbol_key, "side": side, "qty": qty})
        return {"ok": True, "id": tid}

    @capability(
        "markets.portfolio.tx_list", http_method="GET",
        http_path="/markets/portfolio/tx_list", http_tags=["markets"],
        memory="off", silent=True,
        description="List portfolio transactions. Input: symbol_key (str — filter), "
                    "limit (int=200). Output: {transactions:[…]}.",
    )
    async def cap_portfolio_tx_list(symbol_key: str = "", limit: int = 200,
                                    trace_id=None) -> dict:
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                if symbol_key:
                    rows = conn.execute(
                        "SELECT * FROM mkt_portfolio_tx WHERE symbol_key=? "
                        "ORDER BY ts DESC LIMIT ?",
                        (symbol_key, max(1, min(2000, int(limit))))).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM mkt_portfolio_tx ORDER BY ts DESC LIMIT ?",
                        (max(1, min(2000, int(limit))),)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        return {"transactions": rows, "count": len(rows)}

    @capability(
        "markets.portfolio.tx_remove", http_method="POST",
        http_path="/markets/portfolio/tx_remove", http_tags=["markets"], memory="on",
        description="Delete a portfolio transaction. Input: id (str!). Output: {ok}.",
    )
    async def cap_portfolio_tx_remove(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_portfolio_tx WHERE id=?", (id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        return {"ok": True, "id": id}

    def _fifo_positions(txs: List[dict]) -> Dict[str, dict]:
        """FIFO lot accounting per symbol_key."""
        by_key: Dict[str, dict] = {}
        for tx in sorted(txs, key=lambda x: x.get("ts") or ""):
            k = tx["symbol_key"]
            pos = by_key.setdefault(k, {
                "symbol_key": k, "name": tx.get("name") or k.split(":", 1)[-1],
                "asset_class": tx.get("asset_class") or "",
                "lots": [], "realized": 0.0, "fees": 0.0,
                "buy_qty": 0.0, "sell_qty": 0.0,
            })
            pos["fees"] += float(tx.get("fees") or 0)
            qty, px = float(tx["qty"]), float(tx["price"])
            if tx["side"] == "buy":
                pos["lots"].append([qty, px])
                pos["buy_qty"] += qty
            else:
                pos["sell_qty"] += qty
                rem = qty
                while rem > 1e-12 and pos["lots"]:
                    lot = pos["lots"][0]
                    take = min(rem, lot[0])
                    pos["realized"] += take * (px - lot[1])
                    lot[0] -= take
                    rem -= take
                    if lot[0] <= 1e-12:
                        pos["lots"].pop(0)
                if rem > 1e-12:                      # oversell — treat as short-ignored
                    pos["realized"] += rem * px
        for pos in by_key.values():
            q = sum(l[0] for l in pos["lots"])
            cost = sum(l[0] * l[1] for l in pos["lots"])
            pos["qty"] = q
            pos["avg_cost"] = (cost / q) if q > 1e-12 else 0.0
            pos["cost_basis"] = cost
            del pos["lots"]
        return by_key

    @capability(
        "markets.portfolio.positions", http_method="GET",
        http_path="/markets/portfolio/positions", http_tags=["markets"],
        memory="off", silent=True,
        description="Current portfolio: open positions with live valuation from stored "
                    "bars, unrealized + realized P&L and allocation. Input: none. "
                    "Output: {positions:[{symbol_key,name,qty,avg_cost,last,market_value,"
                    "unrealized_pnl,unrealized_pct,realized_pnl,allocation_pct}], "
                    "totals:{value,cost,unrealized,realized}}.",
    )
    async def cap_portfolio_positions(trace_id=None) -> dict:
        await _ensure_tables()
        md = _md()
        r = await cap_portfolio_tx_list(limit=2000)
        by_key = _fifo_positions(r.get("transactions") or [])
        out, tot_val, tot_cost, tot_unrl, tot_rl = [], 0.0, 0.0, 0.0, 0.0
        for k, pos in by_key.items():
            prov, sym = (k.split(":", 1) + [""])[:2] if ":" in k else ("binance", k)
            last = None
            if md:
                try:
                    last = await md.latest_price(prov, sym)
                except Exception:
                    last = None
            mv = (pos["qty"] * last) if (last is not None) else None
            unrl = (mv - pos["cost_basis"]) if mv is not None else None
            entry = {
                "symbol_key": k, "name": pos["name"], "asset_class": pos["asset_class"],
                "qty": round(pos["qty"], 10), "avg_cost": round(pos["avg_cost"], 8),
                "cost_basis": round(pos["cost_basis"], 4),
                "last": last, "market_value": round(mv, 4) if mv is not None else None,
                "unrealized_pnl": round(unrl, 4) if unrl is not None else None,
                "unrealized_pct": round(unrl / pos["cost_basis"] * 100, 3)
                    if (unrl is not None and pos["cost_basis"] > 1e-9) else None,
                "realized_pnl": round(pos["realized"] - pos["fees"], 4),
            }
            out.append(entry)
            tot_rl += entry["realized_pnl"]
            if mv is not None:
                tot_val += mv; tot_unrl += (unrl or 0)
            tot_cost += pos["cost_basis"]
        for e in out:
            e["allocation_pct"] = round(e["market_value"] / tot_val * 100, 2) \
                if (e.get("market_value") and tot_val > 0) else None
        out.sort(key=lambda e: -(e.get("market_value") or 0))
        return {"positions": out,
                "totals": {"value": round(tot_val, 2), "cost": round(tot_cost, 2),
                           "unrealized": round(tot_unrl, 2), "realized": round(tot_rl, 2)},
                "count": len(out)}

    @capability(
        "markets.tax.uk_cgt", http_method="POST",
        http_path="/markets/tax/uk_cgt", http_tags=["markets"], memory="on",
        description="Estimate UK Capital Gains Tax on the portfolio ledger for a tax "
                    "year (6 Apr–5 Apr), applying HMRC share/crypto matching rules in "
                    "order: same-day, 30-day bed-&-breakfast, then the Section 104 "
                    "pool (buy fees added to cost, sell fees deducted from proceeds). "
                    "Input: tax_year (int — starting calendar year, e.g. 2024 = "
                    "2024/25; default current), annual_exempt (float=3000), rate_basic "
                    "(float=18), rate_higher (float=24), basic_band_left (float=0 — £ "
                    "of basic-rate band unused). ESTIMATE, not tax advice. Output: "
                    "{tax_year, proceeds, gains, losses, net_gain, taxable, "
                    "tax_estimate, per_asset:[…], disposals:[…]}.",
    )
    async def cap_tax_uk_cgt(tax_year: int = 0, annual_exempt: float = 3000.0,
                             rate_basic: float = 18.0, rate_higher: float = 24.0,
                             basic_band_left: float = 0.0, trace_id=None) -> dict:
        from datetime import datetime
        await _ensure_tables()
        r = await cap_portfolio_tx_list(limit=2000)
        txs = r.get("transactions") or []
        if not txs:
            return {"error": "no transactions in the ledger to compute CGT on"}

        def _parse(ts):
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                return None

        now = datetime.utcnow()
        if not tax_year:
            tax_year = now.year if (now.month, now.day) >= (4, 6) else now.year - 1
        ty_start, ty_end = datetime(int(tax_year), 4, 6), datetime(int(tax_year) + 1, 4, 6)

        by_asset: Dict[str, list] = {}
        for t in txs:
            d = _parse(t.get("ts"))
            if d is None:
                continue
            try:
                qty, price, fees = float(t.get("qty") or 0), float(t.get("price") or 0), float(t.get("fees") or 0)
            except Exception:
                continue
            if qty <= 0:
                continue
            by_asset.setdefault(t["symbol_key"], []).append(
                {"date": d, "day": d.date(), "side": t.get("side"),
                 "qty": qty, "price": price, "fees": fees})

        disposals: List[dict] = []
        for key, items in by_asset.items():
            items.sort(key=lambda x: x["date"])
            for it in items:
                it["rem"] = it["qty"]
                it["total"] = (it["qty"] * it["price"] + it["fees"]) if it["side"] == "buy" \
                    else (it["qty"] * it["price"] - it["fees"])
            acqs = [x for x in items if x["side"] == "buy"]
            disps = [x for x in items if x["side"] == "sell"]

            def _match(d, a, q, rule):
                cost = a["total"] * (q / a["qty"])
                proceeds = d["total"] * (q / d["qty"])
                a["rem"] -= q; d["rem"] -= q
                disposals.append({"symbol_key": key, "date": d["date"], "qty": q,
                                  "proceeds": round(proceeds, 2), "cost": round(cost, 2),
                                  "gain": round(proceeds - cost, 2), "rule": rule})
            for d in disps:
                for a in acqs:
                    if a["day"] == d["day"] and a["rem"] > 1e-12 and d["rem"] > 1e-12:
                        _match(d, a, min(a["rem"], d["rem"]), "same-day")
            for d in sorted(disps, key=lambda x: x["date"]):
                if d["rem"] <= 1e-12:
                    continue
                for a in sorted(acqs, key=lambda x: x["date"]):
                    if a["rem"] > 1e-12 and 0 < (a["date"] - d["date"]).days <= 30:
                        _match(d, a, min(a["rem"], d["rem"]), "30-day")
                        if d["rem"] <= 1e-12:
                            break
            pool_q, pool_c = 0.0, 0.0
            for it in sorted(items, key=lambda x: x["date"]):
                if it["rem"] <= 1e-12:
                    continue
                if it["side"] == "buy":
                    pool_q += it["rem"]; pool_c += it["total"] * (it["rem"] / it["qty"]); it["rem"] = 0
                else:
                    q = min(it["rem"], pool_q)
                    if q > 1e-12 and pool_q > 1e-12:
                        cost = pool_c * (q / pool_q)
                        proceeds = it["total"] * (q / it["qty"])
                        disposals.append({"symbol_key": key, "date": it["date"], "qty": q,
                                          "proceeds": round(proceeds, 2), "cost": round(cost, 2),
                                          "gain": round(proceeds - cost, 2), "rule": "s104"})
                        pool_q -= q; pool_c -= cost; it["rem"] -= q

        in_year = [d for d in disposals if ty_start <= d["date"] < ty_end]
        proceeds = round(sum(d["proceeds"] for d in in_year), 2)
        gains = round(sum(d["gain"] for d in in_year if d["gain"] > 0), 2)
        losses = round(-sum(d["gain"] for d in in_year if d["gain"] < 0), 2)
        net = round(gains - losses, 2)
        taxable = round(max(0.0, net - float(annual_exempt)), 2)
        band = max(0.0, float(basic_band_left))
        at_basic = min(taxable, band)
        tax = round(at_basic * float(rate_basic) / 100
                    + max(0.0, taxable - band) * float(rate_higher) / 100, 2)

        pa: Dict[str, dict] = {}
        for d in in_year:
            e = pa.setdefault(d["symbol_key"], {"symbol_key": d["symbol_key"],
                              "disposals": 0, "proceeds": 0.0, "gain": 0.0})
            e["disposals"] += 1; e["proceeds"] += d["proceeds"]; e["gain"] += d["gain"]
        per_asset = sorted(({"symbol_key": v["symbol_key"], "disposals": v["disposals"],
                             "proceeds": round(v["proceeds"], 2), "gain": round(v["gain"], 2)}
                            for v in pa.values()), key=lambda x: x["gain"])
        return {"tax_year": f"{tax_year}/{str(tax_year + 1)[-2:]}",
                "window": [ty_start.date().isoformat(), ty_end.date().isoformat()],
                "proceeds": proceeds, "gains": gains, "losses": losses, "net_gain": net,
                "annual_exempt": float(annual_exempt), "taxable": taxable,
                "rate_basic": rate_basic, "rate_higher": rate_higher, "tax_estimate": tax,
                "num_disposals": len(in_year), "per_asset": per_asset,
                "disposals": [{"symbol_key": d["symbol_key"], "date": d["date"].date().isoformat(),
                               "qty": round(d["qty"], 8), "proceeds": d["proceeds"],
                               "cost": d["cost"], "gain": d["gain"], "rule": d["rule"]}
                              for d in sorted(in_year, key=lambda x: x["date"])],
                "disclaimer": "Estimate only — not tax advice. Verify against HMRC guidance."}

    @capability(
        "markets.portfolio.history", http_method="GET",
        http_path="/markets/portfolio/history", http_tags=["markets"],
        memory="off", silent=True,
        description="Daily portfolio value time series (positions replayed against stored "
                    "1d closes). Input: days (int=365). Output: {t:[unix_sec], value:[…], "
                    "cost:[…]}.",
    )
    async def cap_portfolio_history(days: int = 365, trace_id=None) -> dict:
        await _ensure_tables()
        md = _md()
        if not md:
            return {"error": "markets data module unavailable"}
        r = await cap_portfolio_tx_list(limit=2000)
        txs = sorted(r.get("transactions") or [], key=lambda x: x.get("ts") or "")
        if not txs:
            return {"t": [], "value": [], "cost": [], "count": 0}
        days = max(7, min(3650, int(days)))
        now_s = int(time.time())
        start_s = now_s - days * 86400

        # closes per symbol: {key: (t_array, c_array)}
        keys = sorted({tx["symbol_key"] for tx in txs})
        closes: Dict[str, tuple] = {}
        for k in keys:
            prov, sym = (k.split(":", 1) + [""])[:2] if ":" in k else ("binance", k)
            ds = md._dataset_id(prov, sym, "1d")
            bars = await md.get_bars(ds, days + 10, start_ms=(start_s - 86400 * 5) * 1000)
            ts, cs = [], []
            for b in bars:
                tt = b.get("timestamp")
                if tt is None:
                    continue
                ts.append(int(tt // 1000) if tt > 1e12 else int(tt))
                cs.append(float(b.get("close") or 0))
            closes[k] = (ts, cs)

        # daily grid
        t_grid = list(range(start_s - (start_s % 86400), now_s + 1, 86400))
        # position qty over time per key
        out_v, out_c = [], []
        for day in t_grid:
            v_sum, c_sum = 0.0, 0.0
            for k in keys:
                qty, cost = 0.0, 0.0
                lots = []
                for tx in txs:
                    tms = md._iso_to_ms(tx.get("ts") or "")
                    if tms is None or tms / 1000 > day + 86399:
                        continue
                    q, px = float(tx["qty"]), float(tx["price"])
                    if tx["side"] == "buy":
                        lots.append([q, px])
                    else:
                        rem = q
                        while rem > 1e-12 and lots:
                            take = min(rem, lots[0][0])
                            lots[0][0] -= take; rem -= take
                            if lots[0][0] <= 1e-12:
                                lots.pop(0)
                qty = sum(l[0] for l in lots)
                cost = sum(l[0] * l[1] for l in lots)
                if qty <= 1e-12:
                    continue
                ts, cs = closes.get(k, ([], []))
                px = None
                for i in range(len(ts) - 1, -1, -1):
                    if ts[i] <= day + 86399:
                        px = cs[i]
                        break
                if px:
                    v_sum += qty * px
                c_sum += cost
            out_v.append(round(v_sum, 2)); out_c.append(round(c_sum, 2))
        # trim leading empty days
        first = 0
        for i, v in enumerate(out_v):
            if v > 0 or out_c[i] > 0:
                first = i
                break
        return {"t": t_grid[first:], "value": out_v[first:], "cost": out_c[first:],
                "count": len(t_grid) - first}

    log.info("markets lab capabilities loaded (sklearn=%s, backtrader=%s)",
             HAS_SKLEARN, HAS_BACKTRADER)

elif _CAP_AVAILABLE and not HAS_NUMPY:          # pragma: no cover
    log.warning("markets lab disabled — numpy not installed")

