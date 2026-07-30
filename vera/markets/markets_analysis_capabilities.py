"""
markets_analysis_capabilities.py — Indicators, chart annotations & sentiment
============================================================================

The analysis layer of the Markets workbench:

• **Indicator engine** — RSI, stochastics, MACD, EMA/SMA ribbons, Bollinger
  bands, ATR, VWAP, OBV, ROC computed server-side (numpy) from stored bars.
  Per-asset indicator configs live in a small settings KV table so the UI and
  Vera share one source of truth — and Vera can *tweak* them via
  `markets.indicator_config.set`.

• **Annotations** — persistent chart drawings (trendlines, horizontal/vertical
  lines a.k.a. key dates, rectangles, fibs, labels) keyed by asset. Both the
  panel's drawing tools and Vera write through the same caps; every mutation
  emits a `markets.annotate` event so an open chart re-renders live — this is
  how Vera draws on the user's chart.

• **Sentiment** — LLM-scored market sentiment from fresh web headlines, stored
  as a time series per asset, plus `markets.sentiment.map` which returns the
  grid (tracked assets + key market benchmarks) that feeds the heat-map UI.

Feature helpers (`bars_to_arrays`, `compute_indicator`) are exported for the
ML/backtest module.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import re
import sys
import time
import uuid
from typing import Dict, List, Optional

log = logging.getLogger("vera.markets.analysis")

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:                              # pragma: no cover
    HAS_NUMPY = False

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, ollama_generate,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.markets.analysis").warning(
        "markets analysis caps unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Indicator math (pure numpy — no orchestration deps)
# ─────────────────────────────────────────────────────────────────────────────

def bars_to_arrays(bars: List[dict]) -> Dict[str, "np.ndarray"]:
    """List of fabric bar dicts -> {'t','o','h','l','c','v'} float64 arrays."""
    t, o, h, l, c, v = [], [], [], [], [], []
    for d in bars:
        ts = d.get("timestamp")
        if ts is None:
            continue
        t.append(int(ts // 1000) if ts > 1e12 else int(ts))
        cc = float(d.get("close") or 0)
        o.append(float(d.get("open") if d.get("open") is not None else cc))
        h.append(float(d.get("high") if d.get("high") is not None else cc))
        l.append(float(d.get("low") if d.get("low") is not None else cc))
        c.append(cc)
        v.append(float(d.get("volume") or 0))
    return {"t": np.asarray(t, dtype=np.int64),
            "o": np.asarray(o), "h": np.asarray(h), "l": np.asarray(l),
            "c": np.asarray(c), "v": np.asarray(v)}


def _sma(x, n):
    n = max(1, int(n))
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _ema(x, n):
    n = max(1, int(n))
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    k = 2.0 / (n + 1.0)
    e = x[0]
    for i in range(len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    if len(x) >= n:
        out[: n - 1] = np.nan
    return out


def _wilder(x, n):
    """Wilder's smoothing (RSI / ATR)."""
    n = max(1, int(n))
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    acc = np.nanmean(x[:n])
    out[n - 1] = acc
    for i in range(n, len(x)):
        acc = (acc * (n - 1) + x[i]) / n
        out[i] = acc
    return out


def _rsi(c, n=14):
    d = np.diff(c, prepend=c[0] if len(c) else 0.0)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag, al = _wilder(gain, n), _wilder(loss, n)
    rs = np.divide(ag, al, out=np.full(len(c), np.nan), where=(al != 0))
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi[np.isnan(rs) & ~np.isnan(ag)] = 100.0     # zero-loss stretch
    return rsi


def _stoch(h, l, c, k=14, d=3, smooth=3):
    k = max(1, int(k))
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(k - 1, n):
        hh = np.max(h[i - k + 1: i + 1]); ll = np.min(l[i - k + 1: i + 1])
        raw[i] = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100.0
    kline = _sma_nan(raw, smooth)
    dline = _sma_nan(kline, d)
    return kline, dline


def _sma_nan(x, n):
    """SMA that tolerates leading NaNs (stays NaN until n valid values)."""
    n = max(1, int(n))
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1: i + 1]
        w = w[~np.isnan(w)]
        if len(w) == n:
            out[i] = np.mean(w)
    return out


def _macd(c, fast=12, slow=26, signal=9):
    m = _ema(c, fast) - _ema(c, slow)
    s = np.full(len(c), np.nan)
    valid = ~np.isnan(m)
    if valid.any():
        idx = np.where(valid)[0]
        sub = _ema(m[idx], signal)
        s[idx] = sub
    return m, s, m - s


def _bbands(c, n=20, k=2.0):
    mid = _sma(c, n)
    sd = np.full(len(c), np.nan)
    n = max(1, int(n))
    for i in range(n - 1, len(c)):
        sd[i] = np.std(c[i - n + 1: i + 1])
    return mid, mid + k * sd, mid - k * sd


def _atr(h, l, c, n=14):
    pc = np.roll(c, 1); pc[0] = c[0] if len(c) else 0.0
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return _wilder(tr, n)


def _vwap(h, l, c, v, n=0):
    tp = (h + l + c) / 3.0
    if n and int(n) > 0:
        n = int(n)
        out = np.full(len(c), np.nan)
        for i in range(n - 1, len(c)):
            vv = v[i - n + 1: i + 1]; pp = tp[i - n + 1: i + 1]
            s = np.sum(vv)
            out[i] = np.sum(pp * vv) / s if s > 0 else np.nan
        return out
    cv = np.cumsum(v); cpv = np.cumsum(tp * v)
    return np.divide(cpv, cv, out=np.full(len(c), np.nan), where=(cv != 0))


def _obv(c, v):
    d = np.sign(np.diff(c, prepend=c[0] if len(c) else 0.0))
    return np.cumsum(d * v)


def _roc(c, n=10):
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    if len(c) > n:
        prev = c[:-n]
        out[n:] = np.divide(c[n:] - prev, prev, out=np.zeros(len(c) - n),
                            where=(prev != 0)) * 100.0
    return out


def _wma(x, n):
    n = max(1, int(n))
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    w = np.arange(1, n + 1, dtype=np.float64)
    ws = w.sum()
    for i in range(n - 1, len(x)):
        out[i] = np.dot(x[i - n + 1: i + 1], w) / ws
    return out


def _hma(x, n=21):
    n = max(2, int(n))
    half = max(1, n // 2)
    sq = max(1, int(math.sqrt(n)))
    raw = 2.0 * _wma(x, half) - _wma(x, n)
    return _wma(raw, sq)


def _tema(x, n=21):
    e1 = _ema(x, n)
    e2 = _ema(np.nan_to_num(e1, nan=x[0] if len(x) else 0.0), n)
    e3 = _ema(np.nan_to_num(e2, nan=x[0] if len(x) else 0.0), n)
    out = 3 * e1 - 3 * e2 + e3
    out[np.isnan(e1)] = np.nan
    return out


def _donchian(h, l, n=20):
    """Channel of the PRIOR n bars (current bar excluded) so a close can
    actually break above the upper band — turtle-style breakout semantics."""
    n = max(1, int(n))
    up = np.full(len(h), np.nan)
    lo = np.full(len(l), np.nan)
    for i in range(n, len(h)):
        up[i] = np.max(h[i - n: i])
        lo[i] = np.min(l[i - n: i])
    return up, lo


def _keltner(h, l, c, n=20, mult=2.0, atr_n=10):
    mid = _ema(c, n)
    a = _atr(h, l, c, atr_n)
    return mid, mid + mult * a, mid - mult * a


def _supertrend(h, l, c, n=10, mult=3.0):
    a = _atr(h, l, c, n)
    hl2 = (h + l) / 2.0
    ub, lb = hl2 + mult * a, hl2 - mult * a
    n_ = len(c)
    st = np.full(n_, np.nan)
    dir_ = np.full(n_, np.nan)
    fu, fl = np.copy(ub), np.copy(lb)
    started = False
    for i in range(n_):
        if np.isnan(a[i]):
            continue
        if not started:
            st[i], dir_[i] = lb[i], 1.0
            started = True
            continue
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if dir_[i - 1] == 1.0:
            if c[i] < fl[i - 1]:
                dir_[i], st[i] = -1.0, fu[i]
            else:
                dir_[i], st[i] = 1.0, fl[i]
        else:
            if c[i] > fu[i - 1]:
                dir_[i], st[i] = 1.0, fl[i]
            else:
                dir_[i], st[i] = -1.0, fu[i]
    return st, dir_


def _ichimoku(h, l, c, conv=9, base=26, spanb=52, disp=26):
    def _mid(k):
        k = max(1, int(k))
        out = np.full(len(c), np.nan)
        for i in range(k - 1, len(c)):
            out[i] = (np.max(h[i - k + 1: i + 1]) + np.min(l[i - k + 1: i + 1])) / 2.0
        return out

    def _fwd(x, k):
        out = np.full(len(x), np.nan)
        if k < len(x):
            out[k:] = x[: len(x) - k]
        return out
    tenkan, kijun = _mid(conv), _mid(base)
    disp = max(0, int(disp))
    senkou_a = _fwd((tenkan + kijun) / 2.0, disp)
    senkou_b = _fwd(_mid(spanb), disp)
    chikou = np.full(len(c), np.nan)
    if disp < len(c):
        chikou[: len(c) - disp] = c[disp:]
    return tenkan, kijun, senkou_a, senkou_b, chikou


def _cci(h, l, c, n=20):
    tp = (h + l + c) / 3.0
    m = _sma(tp, n)
    n = max(1, int(n))
    md = np.full(len(c), np.nan)
    for i in range(n - 1, len(c)):
        w = tp[i - n + 1: i + 1]
        md[i] = np.mean(np.abs(w - np.mean(w)))
    return np.divide(tp - m, 0.015 * md, out=np.full(len(c), np.nan),
                     where=(md > 1e-18))


def _mfi(h, l, c, v, n=14):
    tp = (h + l + c) / 3.0
    mf = tp * v
    d = np.diff(tp, prepend=tp[0] if len(tp) else 0.0)
    pos = np.where(d > 0, mf, 0.0)
    neg = np.where(d < 0, mf, 0.0)
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        ps = np.sum(pos[i - n + 1: i + 1])
        ns = np.sum(neg[i - n + 1: i + 1])
        out[i] = 100.0 if ns <= 1e-18 else 100.0 - 100.0 / (1.0 + ps / ns)
    return out


def _willr(h, l, c, n=14):
    n = max(1, int(n))
    out = np.full(len(c), np.nan)
    for i in range(n - 1, len(c)):
        hh = np.max(h[i - n + 1: i + 1])
        ll = np.min(l[i - n + 1: i + 1])
        out[i] = -50.0 if hh == ll else (hh - c[i]) / (hh - ll) * -100.0
    return out


def _adx(h, l, c, n=14):
    up = np.diff(h, prepend=h[0] if len(h) else 0.0)
    dn = -np.diff(l, prepend=l[0] if len(l) else 0.0)
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = np.roll(c, 1)
    pc[0] = c[0] if len(c) else 0.0
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr_w = _wilder(tr, n)
    pdi = 100.0 * np.divide(_wilder(plus_dm, n), atr_w,
                            out=np.full(len(c), np.nan), where=(atr_w > 1e-18))
    mdi = 100.0 * np.divide(_wilder(minus_dm, n), atr_w,
                            out=np.full(len(c), np.nan), where=(atr_w > 1e-18))
    s = pdi + mdi
    dx = 100.0 * np.divide(np.abs(pdi - mdi), s,
                           out=np.full(len(c), np.nan), where=(s > 1e-18))
    valid = ~np.isnan(dx)
    adx = np.full(len(c), np.nan)
    if valid.any():
        idx = np.where(valid)[0]
        adx[idx] = _wilder(dx[idx], n)
    return adx, pdi, mdi


def _psar(h, l, af0=0.02, max_af=0.2):
    n_ = len(h)
    out = np.full(n_, np.nan)
    if n_ < 3:
        return out
    up = True
    af = af0
    ep = h[0]
    sar = l[0]
    for i in range(1, n_):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], af0
            elif h[i] > ep:
                ep = h[i]
                af = min(max_af, af + af0)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], af0
            elif l[i] < ep:
                ep = l[i]
                af = min(max_af, af + af0)
        out[i] = sar
    return out


def _zigzag_causal(c, pct=5.0, atr=None, mult=0.0):
    """CAUSAL (non-repainting) zigzag state per bar — safe for backtesting.
    At bar i: pv_high/pv_low = price of the last CONFIRMED pivot high/low
    (confirmation = reversal beyond the threshold from the running extreme),
    pv_dir = current confirmed leg (+1 rising / -1 falling). Unlike a plotted
    zigzag, nothing here looks ahead: levels/direction only change on the bar
    where the reversal condition is first met. Threshold is pct% of the
    extreme, or mult×ATR when mult>0 and an ATR series is supplied."""
    n = len(c)
    thr_pct = max(0.05, float(pct)) / 100.0
    pv_high = np.full(n, np.nan)
    pv_low = np.full(n, np.nan)
    pv_dir = np.full(n, np.nan)
    if n < 3:
        return pv_high, pv_low, pv_dir

    def thr(i, px):
        if mult and atr is not None:
            a = atr[i]
            return float("inf") if math.isnan(a) else max(1e-12, float(mult) * a)
        return abs(px) * thr_pct
    hi_i = lo_i = ext_i = 0
    direction = 0
    last_high = last_low = np.nan
    for i in range(n):
        if i > 0:
            if direction == 0:
                if c[i] > c[hi_i]:
                    hi_i = i
                if c[i] < c[lo_i]:
                    lo_i = i
                if (c[i] - c[lo_i]) >= thr(i, c[lo_i]):
                    last_low, direction, ext_i = c[lo_i], 1, i
                elif (c[hi_i] - c[i]) >= thr(i, c[hi_i]):
                    last_high, direction, ext_i = c[hi_i], -1, i
            elif direction == 1:
                if c[i] > c[ext_i]:
                    ext_i = i
                elif (c[ext_i] - c[i]) >= thr(i, c[ext_i]):
                    last_high, direction, ext_i = c[ext_i], -1, i
            else:
                if c[i] < c[ext_i]:
                    ext_i = i
                elif (c[i] - c[ext_i]) >= thr(i, c[ext_i]):
                    last_low, direction, ext_i = c[ext_i], 1, i
        pv_high[i] = last_high
        pv_low[i] = last_low
        pv_dir[i] = direction if direction != 0 else np.nan
    return pv_high, pv_low, pv_dir


def _zscore(c, n=20):
    m = _sma(c, n)
    n = max(1, int(n))
    sd = np.full(len(c), np.nan)
    for i in range(n - 1, len(c)):
        sd[i] = np.std(c[i - n + 1: i + 1])
    return np.divide(c - m, sd, out=np.full(len(c), np.nan), where=(sd > 1e-18))


RIBBON_DEFAULT = [8, 13, 21, 34, 55]

# kind -> (pane, default params)
INDICATOR_DEFS = {
    "sma":    ("main", {"n": 50}),
    "ema":    ("main", {"n": 20}),
    "hma":    ("main", {"n": 21}),
    "tema":   ("main", {"n": 21}),
    "ribbon": ("main", {"periods": RIBBON_DEFAULT, "ma": "ema"}),
    "bbands": ("main", {"n": 20, "k": 2.0}),
    "keltner": ("main", {"n": 20, "mult": 2.0, "atr_n": 10}),
    "donchian": ("main", {"n": 20}),
    "supertrend": ("main", {"n": 10, "mult": 3.0}),
    "ichimoku": ("main", {"conv": 9, "base": 26, "spanb": 52, "disp": 26}),
    "psar":   ("main", {"af": 0.02, "max_af": 0.2}),
    "pivotlevels": ("main", {"pct": 5.0, "mult": 0.0, "atr_n": 14}),
    "pivotdir":    ("sub",  {"pct": 5.0, "mult": 0.0, "atr_n": 14}),
    "vwap":   ("main", {"n": 0}),
    "rsi":    ("sub",  {"n": 14}),
    "stoch":  ("sub",  {"k": 14, "d": 3, "smooth": 3}),
    "macd":   ("sub",  {"fast": 12, "slow": 26, "signal": 9}),
    "atr":    ("sub",  {"n": 14}),
    "adx":    ("sub",  {"n": 14}),
    "cci":    ("sub",  {"n": 20}),
    "mfi":    ("sub",  {"n": 14}),
    "willr":  ("sub",  {"n": 14}),
    "zscore": ("sub",  {"n": 20}),
    "obv":    ("sub",  {}),
    "roc":    ("sub",  {"n": 10}),
}


def compute_indicator(arr: Dict[str, "np.ndarray"], kind: str,
                      params: dict = None) -> Dict[str, "np.ndarray"]:
    """Compute one indicator; returns {series_name: ndarray} aligned to arr['t']."""
    p = dict(INDICATOR_DEFS.get(kind, ("sub", {}))[1])
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    h, l, c, v = arr["h"], arr["l"], arr["c"], arr["v"]
    if kind == "sma":
        return {f"sma{p['n']}": _sma(c, p["n"])}
    if kind == "ema":
        return {f"ema{p['n']}": _ema(c, p["n"])}
    if kind == "ribbon":
        fn = _sma if str(p.get("ma", "ema")).lower() == "sma" else _ema
        periods = p.get("periods") or RIBBON_DEFAULT
        if isinstance(periods, str):
            periods = [int(x) for x in re.findall(r"\d+", periods)] or RIBBON_DEFAULT
        return {f"{p.get('ma','ema')}{n}": fn(c, int(n)) for n in periods[:10]}
    if kind == "bbands":
        mid, up, lo = _bbands(c, p["n"], float(p["k"]))
        return {"bb_mid": mid, "bb_upper": up, "bb_lower": lo}
    if kind == "vwap":
        return {"vwap": _vwap(h, l, c, v, p.get("n", 0))}
    if kind == "rsi":
        return {"rsi": _rsi(c, p["n"])}
    if kind == "stoch":
        k, d = _stoch(h, l, c, p["k"], p["d"], p["smooth"])
        return {"stoch_k": k, "stoch_d": d}
    if kind == "macd":
        m, s, hst = _macd(c, p["fast"], p["slow"], p["signal"])
        return {"macd": m, "macd_signal": s, "macd_hist": hst}
    if kind == "atr":
        return {"atr": _atr(h, l, c, p["n"])}
    if kind == "obv":
        return {"obv": _obv(c, v)}
    if kind == "roc":
        return {"roc": _roc(c, p["n"])}
    if kind == "hma":
        return {f"hma{p['n']}": _hma(c, p["n"])}
    if kind == "tema":
        return {f"tema{p['n']}": _tema(c, p["n"])}
    if kind == "donchian":
        up, lo = _donchian(h, l, p["n"])
        return {"dc_upper": up, "dc_mid": (up + lo) / 2.0, "dc_lower": lo}
    if kind == "keltner":
        mid, up, lo = _keltner(h, l, c, p["n"], float(p["mult"]), p["atr_n"])
        return {"kc_mid": mid, "kc_upper": up, "kc_lower": lo}
    if kind == "supertrend":
        st, dir_ = _supertrend(h, l, c, p["n"], float(p["mult"]))
        return {"supertrend": st, "st_dir": dir_}
    if kind == "ichimoku":
        tenkan, kijun, sa, sb, chikou = _ichimoku(
            h, l, c, p["conv"], p["base"], p["spanb"], p["disp"])
        return {"tenkan": tenkan, "kijun": kijun, "senkou_a": sa,
                "senkou_b": sb, "chikou": chikou}
    if kind == "psar":
        return {"psar": _psar(h, l, float(p["af"]), float(p["max_af"]))}
    if kind in ("pivotlevels", "pivotdir"):
        atr = _atr(h, l, c, p.get("atr_n", 14)) if float(p.get("mult") or 0) > 0 else None
        pv_h, pv_l, pv_d = _zigzag_causal(c, float(p.get("pct", 5.0)), atr,
                                          float(p.get("mult") or 0))
        if kind == "pivotlevels":
            return {"pv_high": pv_h, "pv_low": pv_l}
        return {"pv_dir": pv_d}
    if kind == "cci":
        return {"cci": _cci(h, l, c, p["n"])}
    if kind == "mfi":
        return {"mfi": _mfi(h, l, c, v, p["n"])}
    if kind == "willr":
        return {"willr": _willr(h, l, c, p["n"])}
    if kind == "adx":
        adx, pdi, mdi = _adx(h, l, c, p["n"])
        return {"adx": adx, "plus_di": pdi, "minus_di": mdi}
    if kind == "zscore":
        return {"zscore": _zscore(c, p["n"])}
    raise ValueError(f"unknown indicator kind '{kind}'")


def _series_json(a: "np.ndarray") -> List[Optional[float]]:
    a = np.asarray(a, dtype=float)
    return np.where(np.isnan(a), None, np.round(a, 8)).tolist()


# ─────────────────────────────────────────────────────────────────────────────
# Custom indicator expressions (AST-sandboxed — no builtins, whitelisted nodes)
# ─────────────────────────────────────────────────────────────────────────────
#
# An expression works on the bar arrays `o h l c v t` with a small vector
# function library, e.g.:
#   (ema(c, 9) - ema(c, 21)) / c * 100
#   where(cross_up(sma(c,10), sma(c,30)), 1, 0)
#   (c - lowest(l, 20)) / (highest(h, 20) - lowest(l, 20)) * 100
# Comparisons produce 0/1 masks; combine them with & and | (parenthesised).

_EXPR_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Call, ast.keyword,
    ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
    ast.BitAnd, ast.BitOr, ast.Invert,
    ast.UAdd, ast.USub,
    ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq,
)


class SafeExprError(ValueError):
    pass


def compile_expr(expr: str):
    """Parse + validate a custom-indicator expression; returns compiled code."""
    expr = (expr or "").strip()
    if not expr:
        raise SafeExprError("empty expression")
    if len(expr) > 800:
        raise SafeExprError("expression too long (max 800 chars)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise SafeExprError(f"syntax error: {e.msg}")
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_ALLOWED_NODES):
            raise SafeExprError(f"'{type(node).__name__}' is not allowed in expressions")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise SafeExprError("only plain function calls like ema(c, 20) are allowed")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise SafeExprError("only numeric constants are allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise SafeExprError("dunder names are not allowed")
    return compile(tree, "<indicator>", "eval")


def _expr_env(arr: Dict[str, "np.ndarray"]) -> dict:
    """Variable + function environment for one bar-array set."""
    o, h, l, c, v = arr["o"], arr["h"], arr["l"], arr["c"], arr["v"]
    t = arr["t"].astype(np.float64)

    def shift(x, n=1):
        n = int(n)
        out = np.full(len(x), np.nan)
        if n >= 0:
            out[n:] = np.asarray(x, dtype=np.float64)[: len(x) - n] if n else x
        else:
            out[:n] = np.asarray(x, dtype=np.float64)[-n:]
        return out

    def _roll(x, n, fn):
        n = max(1, int(n))
        out = np.full(len(x), np.nan)
        xx = np.asarray(x, dtype=np.float64)
        for i in range(n - 1, len(xx)):
            out[i] = fn(xx[i - n + 1: i + 1])
        return out

    def rsi_fn(*args):
        if len(args) == 1:
            return _rsi(c, int(args[0]))
        return _rsi(np.asarray(args[0], dtype=np.float64), int(args[1]))

    def cross_up(a, b):
        a = np.asarray(a, dtype=np.float64); b = np.asarray(b, dtype=np.float64)
        pa, pb = np.roll(a, 1), np.roll(b, 1)
        m = (pa <= pb) & (a > b) & ~np.isnan(a) & ~np.isnan(b) & ~np.isnan(pa) & ~np.isnan(pb)
        m[0] = False
        return m.astype(np.float64)

    env = {
        "o": o, "h": h, "l": l, "c": c, "v": v, "t": t,
        "open": o, "high": h, "low": l, "close": c, "volume": v,
        "sma": lambda x, n: _sma(np.asarray(x, dtype=np.float64), n),
        "ema": lambda x, n: _ema(np.asarray(x, dtype=np.float64), n),
        "wilder": lambda x, n: _wilder(np.asarray(x, dtype=np.float64), n),
        "stdev": lambda x, n: _roll(x, n, np.std),
        "highest": lambda x, n: _roll(x, n, np.max),
        "lowest": lambda x, n: _roll(x, n, np.min),
        "median": lambda x, n: _roll(x, n, np.median),
        "sum": lambda x, n: _roll(x, n, np.sum),
        "rsi": rsi_fn,
        "atr": lambda n=14: _atr(h, l, c, n),
        "tr": lambda: np.maximum(h - l, np.maximum(np.abs(h - shift(c, 1)),
                                                   np.abs(l - shift(c, 1)))),
        "vwap": lambda n=0: _vwap(h, l, c, v, n),
        "obv": lambda: _obv(c, v),
        "roc": lambda x, n: _roc(np.asarray(x, dtype=np.float64), n),
        "shift": shift, "lag": shift,
        "cross_up": cross_up,
        "cross_dn": lambda a, b: cross_up(b, a),
        "abs": np.abs, "log": lambda x: np.log(np.maximum(1e-12, x)),
        "sqrt": lambda x: np.sqrt(np.maximum(0.0, x)), "sign": np.sign,
        "clip": np.clip,
        "where": lambda cond, a, b: np.where(np.asarray(cond, dtype=bool), a, b),
        "nz": lambda x, val=0.0: np.nan_to_num(np.asarray(x, dtype=np.float64), nan=val),
        "nan": float("nan"),
    }
    return env


def eval_custom_series(arr: Dict[str, "np.ndarray"],
                       series: Dict[str, str]) -> Dict[str, "np.ndarray"]:
    """Evaluate {series_name: expression} against one bar-array set."""
    env = _expr_env(arr)
    n = len(arr["c"])
    out = {}
    for name, expr in list(series.items())[:6]:
        code = compile_expr(expr)
        try:
            val = eval(code, {"__builtins__": {}}, env)   # nosec — AST whitelisted above
        except SafeExprError:
            raise
        except Exception as e:
            raise SafeExprError(f"{name}: evaluation failed: {e}")
        if isinstance(val, (int, float)):
            val = np.full(n, float(val))
        val = np.asarray(val, dtype=np.float64)
        if val.shape != (n,):
            raise SafeExprError(f"{name}: expression must produce one value per bar")
        out[str(name)[:32]] = val
    if not out:
        raise SafeExprError("no series defined")
    return out


DEFAULT_INDICATOR_CONFIG = {
    "indicators": [
        {"id": "ribbon", "kind": "ribbon", "params": {"periods": RIBBON_DEFAULT, "ma": "ema"}, "enabled": False},
        {"id": "bbands", "kind": "bbands", "params": {"n": 20, "k": 2.0}, "enabled": False},
        {"id": "vwap",   "kind": "vwap",   "params": {"n": 0},  "enabled": False},
        {"id": "rsi",    "kind": "rsi",    "params": {"n": 14}, "enabled": True},
        {"id": "stoch",  "kind": "stoch",  "params": {"k": 14, "d": 3, "smooth": 3}, "enabled": False},
        {"id": "macd",   "kind": "macd",   "params": {"fast": 12, "slow": 26, "signal": 9}, "enabled": True},
    ],
    "volume": True,
}

ANNOTATION_KINDS = {"trendline", "ray", "hline", "vline", "rect", "fib", "label", "arrow"}

KEY_MARKET_ASSETS = [
    {"key": "yahoo:^GSPC",   "name": "S&P 500",  "query": "S&P 500 stock market outlook"},
    {"key": "yahoo:^IXIC",   "name": "Nasdaq",   "query": "Nasdaq tech stocks outlook"},
    {"key": "yahoo:^VIX",    "name": "VIX",      "query": "VIX volatility index market fear"},
    {"key": "yahoo:GC=F",    "name": "Gold",     "query": "gold price outlook"},
    {"key": "yahoo:CL=F",    "name": "Crude Oil","query": "crude oil price outlook"},
    {"key": "yahoo:DX-Y.NYB","name": "US Dollar","query": "US dollar index DXY outlook"},
    {"key": "yahoo:BTC-USD", "name": "Bitcoin",  "query": "bitcoin BTC price news"},
    {"key": "yahoo:ETH-USD", "name": "Ethereum", "query": "ethereum ETH price news"},
]


def _slug(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (symbol or "").lower()).strip("_") or "asset"


def norm_symbol_key(key: str) -> str:
    """'binance:BTC/USDT' and 'binance:btc_usdt' -> 'binance:btc_usdt' — one
    settings key regardless of whether the caller had the display symbol or a
    dataset-derived slug."""
    key = (key or "").strip()
    if ":" not in key:
        return _slug(key)
    prov, sym = key.split(":", 1)
    return f"{prov.strip().lower()}:{_slug(sym)}"


def sentiment_query_for(key: str, name: str = "") -> str:
    """Build a news-search query for an asset key like 'binance:BTC/USDT'."""
    for k in KEY_MARKET_ASSETS:
        if k["key"] == key:
            return k["query"]
    prov, sym = (key.split(":", 1) + [""])[:2] if ":" in key else ("", key)
    base = name or sym
    if prov in ("binance", "coinbase", "kraken", "bybit"):
        base = sym.split("/")[0]
        return f"{base} crypto price news"
    if prov == "custom":
        return f"{base} price value news"
    return f"{base} stock news"


def parse_llm_json(text: str) -> Optional[dict]:
    """Tolerant JSON extraction from an LLM reply."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE and HAS_NUMPY:

    def _md():
        return sys.modules.get("markets_data_capabilities")

    def _mc():
        return sys.modules.get("markets_capabilities")

    # ── settings / annotations / sentiment tables ────────────────────────────

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
                CREATE TABLE IF NOT EXISTS mkt_annotations (
                    id         TEXT PRIMARY KEY,
                    symbol_key TEXT NOT NULL,
                    timeframe  TEXT DEFAULT '',
                    kind       TEXT,
                    points     TEXT,
                    text       TEXT,
                    color      TEXT,
                    author     TEXT,
                    meta       TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS mkt_ann_sym ON mkt_annotations(symbol_key)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_custom_indicators (
                    id          TEXT PRIMARY KEY,
                    name        TEXT,
                    pane        TEXT DEFAULT 'sub',
                    series      TEXT,
                    color       TEXT,
                    author      TEXT,
                    description TEXT,
                    created_at  TEXT,
                    updated_at  TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mkt_sentiment (
                    id         TEXT PRIMARY KEY,
                    key        TEXT NOT NULL,
                    name       TEXT,
                    score      REAL,
                    confidence REAL,
                    label      TEXT,
                    summary    TEXT,
                    headlines  TEXT,
                    created_at TEXT
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS mkt_sent_key ON mkt_sentiment(key)")
            conn.commit()
        finally:
            conn.close()

    async def _ensure_tables():
        global _TABLES_READY
        if _TABLES_READY:
            return
        await asyncio.get_running_loop().run_in_executor(None, _ensure_tables_sync)
        _TABLES_READY = True

    def _custom_ind_rows_sync() -> List[dict]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM mkt_custom_indicators ORDER BY created_at").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["series"] = json.loads(d.get("series") or "{}")
                except Exception:
                    d["series"] = {}
                out.append(d)
            return out
        finally:
            conn.close()

    def _custom_ind_get_sync(ident: str) -> Optional[dict]:
        conn = _sqlite_conn()
        try:
            r = conn.execute(
                "SELECT * FROM mkt_custom_indicators WHERE id=? OR name=?",
                (ident, ident)).fetchone()
            if not r:
                return None
            d = dict(r)
            try:
                d["series"] = json.loads(d.get("series") or "{}")
            except Exception:
                d["series"] = {}
            return d
        finally:
            conn.close()

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

    # ── Indicators ────────────────────────────────────────────────────────────

    async def _load_arrays(dataset_id: str, limit: int, start: str = "", end: str = ""):
        md = _md()
        if not md:
            return None, {"error": "markets data module unavailable"}
        bars = await md.get_bars(dataset_id, limit,
                                 md._iso_to_ms(start), md._iso_to_ms(end))
        if not bars:
            return None, {"error": f"no bars stored for {dataset_id}"}
        return bars_to_arrays(bars), None

    @capability(
        "markets.indicators", http_method="POST", http_path="/markets/indicators",
        http_tags=["markets"], memory="off", silent=True,
        description="Compute technical indicators from stored bars, server-side. "
                    "Input: dataset_id (str!), indicators (list of {kind, params, id} — "
                    "kinds: sma|ema|ribbon|bbands|vwap|rsi|stoch|macd|atr|obv|roc; "
                    "omit to use the asset's saved config), limit (int=3000), "
                    "start/end (ISO str). Output: {t:[unix_sec], indicators:[{id,kind,"
                    "params,pane,series:{name:[...]}}]} — series align with t.",
    )
    async def cap_markets_indicators(dataset_id: str = "", indicators=None,
                                     limit: int = 3000, start: str = "", end: str = "",
                                     trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        await _ensure_tables()
        arr, err = await _load_arrays(dataset_id, max(50, min(50_000, int(limit))), start, end)
        if err:
            return err

        specs = indicators
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except Exception:
                specs = None
        if not specs:
            # derive symbol_key from dataset: mkt.{prov}.{slug}.{tf}
            parts = dataset_id.split(".")
            sym_key = f"{parts[1]}:{parts[2]}" if len(parts) >= 4 else dataset_id
            cfg = await _indicator_config_for(sym_key)
            specs = [i for i in cfg.get("indicators", []) if i.get("enabled")]

        out = []
        loop = asyncio.get_running_loop()
        for spec in specs[:16]:
            kind = str(spec.get("kind") or spec.get("id") or "")
            params = spec.get("params") or {}
            item = {"id": spec.get("id") or kind, "kind": kind}
            if spec.get("color"):
                item["color"] = spec["color"]
            if kind.lower() in INDICATOR_DEFS:
                kind = kind.lower()

                def _compute_sync(kind=kind, params=params):
                    series = compute_indicator(arr, kind, params)
                    return {k: _series_json(v) for k, v in series.items()}
                try:
                    ser = await loop.run_in_executor(None, _compute_sync)
                except Exception as e:
                    out.append({**item, "error": str(e)[:200]})
                    continue
                eff = dict(INDICATOR_DEFS[kind][1]); eff.update(params or {})
                item.update({"kind": kind, "params": eff, "pane": INDICATOR_DEFS[kind][0],
                             "series": ser})
                out.append(item)
                continue
            # custom indicator — by id or name
            row = await loop.run_in_executor(None, _custom_ind_get_sync,
                                             str(params.get("id") or kind))
            if not row:
                out.append({**item, "error": f"unknown indicator '{kind}'"})
                continue
            def _custom_sync(exprs=row["series"]):
                series = eval_custom_series(arr, exprs)
                return {k: _series_json(v) for k, v in series.items()}
            try:
                ser = await loop.run_in_executor(None, _custom_sync)
            except SafeExprError as e:
                out.append({**item, "error": str(e)[:200]})
                continue
            item.update({
                "kind": row["id"], "custom": True, "label": row.get("name"),
                "params": {}, "pane": spec.get("pane") or row.get("pane") or "sub",
                "color": spec.get("color") or row.get("color") or "",
                "series": ser,
            })
            out.append(item)
        return {"dataset_id": dataset_id, "count": int(len(arr["t"])),
                "t": arr["t"].tolist(), "indicators": out}

    async def _merge_customs(cfg: dict) -> dict:
        """Append registered custom indicators (disabled) that the config
        doesn't mention yet, so UIs and agents always see them as options."""
        try:
            rows = await asyncio.get_running_loop().run_in_executor(
                None, _custom_ind_rows_sync)
        except Exception:
            return cfg
        have = {i.get("id") for i in cfg.get("indicators", [])}
        for r in rows:
            if r["id"] in have:
                # refresh display metadata on existing entries
                for i in cfg["indicators"]:
                    if i.get("id") == r["id"]:
                        i["custom"] = True
                        i.setdefault("label", r.get("name"))
                        i.setdefault("color", r.get("color") or "")
                        i["pane"] = i.get("pane") or r.get("pane") or "sub"
                continue
            cfg.setdefault("indicators", []).append({
                "id": r["id"], "kind": r["id"], "custom": True,
                "label": r.get("name"), "pane": r.get("pane") or "sub",
                "color": r.get("color") or "", "params": {}, "enabled": False,
            })
        return cfg

    async def _indicator_config_for(symbol_key: str) -> dict:
        loop = asyncio.get_running_loop()
        cfg = await loop.run_in_executor(
            None, _setting_get_sync, f"ind:{norm_symbol_key(symbol_key)}")
        if not cfg:
            cfg = await loop.run_in_executor(None, _setting_get_sync, "ind:default")
        cfg = cfg or json.loads(json.dumps(DEFAULT_INDICATOR_CONFIG))
        return await _merge_customs(cfg)

    @capability(
        "markets.indicator_config.get", http_method="GET",
        http_path="/markets/indicator_config", http_tags=["markets"],
        memory="off", silent=True,
        description="Get the saved indicator config for an asset (falls back to the "
                    "default set). Input: symbol_key (str — 'provider:symbol', e.g. "
                    "'binance:BTC/USDT'; blank = default config). "
                    "Output: {symbol_key, config:{indicators:[{id,kind,params,enabled}],volume}}.",
    )
    async def cap_indicator_config_get(symbol_key: str = "", trace_id=None) -> dict:
        await _ensure_tables()
        if not symbol_key:
            loop = asyncio.get_running_loop()
            cfg = await loop.run_in_executor(None, _setting_get_sync, "ind:default")
            cfg = await _merge_customs(cfg or json.loads(json.dumps(DEFAULT_INDICATOR_CONFIG)))
            return {"symbol_key": "", "config": cfg}
        return {"symbol_key": symbol_key, "config": await _indicator_config_for(symbol_key)}

    @capability(
        "markets.indicator_config.set", http_method="POST",
        http_path="/markets/indicator_config/set", http_tags=["markets"], memory="on",
        description="Save/tweak an asset's indicator config (which indicators are on and "
                    "their parameters — RSI period, MACD fast/slow/signal, ribbon periods, "
                    "stochastic k/d, Bollinger n/k…). Vera uses this to tune what the chart "
                    "shows. Input: symbol_key (str — blank = set the default config), "
                    "config (object! — {indicators:[{id,kind,params,enabled}],volume}), "
                    "merge (bool=True — merge per-indicator over the existing config). "
                    "Output: {ok, symbol_key, config}.",
    )
    async def cap_indicator_config_set(symbol_key: str = "", config=None,
                                       merge: bool = True, trace_id=None) -> dict:
        await _ensure_tables()
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except Exception:
                return {"error": "config must be a JSON object"}
        if not isinstance(config, dict):
            return {"error": "config object required"}
        base = await _indicator_config_for(symbol_key) if merge else \
            json.loads(json.dumps(DEFAULT_INDICATOR_CONFIG))
        if merge:
            by_id = {i.get("id"): i for i in base.get("indicators", [])}
            for inc in config.get("indicators", []):
                iid = inc.get("id") or inc.get("kind")
                if iid in by_id:
                    cur = by_id[iid]
                    cur.update({k: v for k, v in inc.items() if k != "params"})
                    if inc.get("params"):
                        cur.setdefault("params", {}).update(inc["params"])
                else:
                    inc.setdefault("id", iid)
                    base.setdefault("indicators", []).append(inc)
            for k, v in config.items():
                if k != "indicators":
                    base[k] = v
            final = base
        else:
            final = config
        key = f"ind:{norm_symbol_key(symbol_key)}" if symbol_key else "ind:default"
        await asyncio.get_running_loop().run_in_executor(None, _setting_set_sync, key, final)
        await emit_event({"type": "markets.indicators", "stage": "config",
                          "symbol_key": symbol_key})
        return {"ok": True, "symbol_key": symbol_key, "config": final}

    # ── Custom indicators (user- and Vera-authored) ───────────────────────────

    @capability(
        "markets.indicator.custom.save", http_method="POST",
        http_path="/markets/indicator/custom/save", http_tags=["markets"], memory="on",
        description="Create or update a CUSTOM indicator from a math expression over the "
                    "bar arrays o/h/l/c/v — this is how new indicators are built in the UI "
                    "and how Vera invents indicators. Functions: sma(x,n) ema(x,n) "
                    "wilder(x,n) stdev(x,n) highest(x,n) lowest(x,n) median(x,n) sum(x,n) "
                    "rsi(n) atr(n) tr() vwap(n) obv() roc(x,n) shift(x,n) cross_up(a,b) "
                    "cross_dn(a,b) abs log sqrt sign clip(x,a,b) where(cond,a,b) nz(x). "
                    "Comparisons give 0/1 masks; combine with & and |. Examples: "
                    "'(ema(c,9)-ema(c,21))/c*100' or '(c-lowest(l,20))/(highest(h,20)-"
                    "lowest(l,20))*100'. Input: name (str!), expr (str — single-series "
                    "shorthand) OR series (object {series_name: expr, …} up to 4), "
                    "pane (main|sub, default sub), color (css str), id (str — update "
                    "existing), description (str), author (str='vera'). "
                    "Output: {ok, id, sample} — sample shows the last values so you can "
                    "sanity-check the math.",
    )
    async def cap_custom_ind_save(name: str = "", expr: str = "", series=None,
                                  pane: str = "sub", color: str = "", id: str = "",
                                  description: str = "", author: str = "vera",
                                  trace_id=None) -> dict:
        if not name.strip():
            return {"error": "name required"}
        await _ensure_tables()
        if isinstance(series, str):
            try:
                series = json.loads(series)
            except Exception:
                series = None
        if not isinstance(series, dict) or not series:
            if not (expr or "").strip():
                return {"error": "expr or series required"}
            series = {_slug(name)[:20] or "value": expr.strip()}
        series = {str(k)[:32]: str(v) for k, v in list(series.items())[:4]}
        # validate: compile + evaluate on a small synthetic series
        try:
            for e in series.values():
                compile_expr(e)
            n = 120
            synth = {
                "t": np.arange(n) * 86400 + 1_600_000_000,
                "o": np.linspace(99, 119, n), "h": np.linspace(101, 121, n),
                "l": np.linspace(98, 118, n), "c": np.linspace(100, 120, n) +
                     np.sin(np.arange(n) / 5.0) * 2.0,
                "v": np.full(n, 1000.0),
            }
            synth = {k: np.asarray(v, dtype=np.float64) if k != "t" else
                     np.asarray(v, dtype=np.int64) for k, v in synth.items()}
            sample_series = eval_custom_series(synth, series)
        except SafeExprError as e:
            return {"error": str(e)}
        cid = id or ("cx_" + uuid.uuid4().hex[:8])
        pane = pane if pane in ("main", "sub") else "sub"

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO mkt_custom_indicators "
                    "(id,name,pane,series,color,author,description,created_at,updated_at) VALUES "
                    "(?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM mkt_custom_indicators "
                    "WHERE id=?),?),?)",
                    (cid, name.strip(), pane, json.dumps(series), color or "",
                     author or "vera", description or "", cid, now_iso(), now_iso()))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.indicators", "stage": "custom",
                          "id": cid, "name": name.strip()})
        return {"ok": True, "id": cid, "pane": pane,
                "sample": {k: [None if math.isnan(x) else round(float(x), 6)
                               for x in v[-5:]] for k, v in sample_series.items()}}

    @capability(
        "markets.indicator.custom.list", http_method="GET",
        http_path="/markets/indicator/custom/list", http_tags=["markets"],
        memory="off", silent=True,
        description="List custom indicators. Output: {indicators:[{id,name,pane,series,"
                    "color,author,description}]}.",
    )
    async def cap_custom_ind_list(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(None, _custom_ind_rows_sync)
        return {"indicators": rows, "count": len(rows)}

    @capability(
        "markets.indicator.custom.delete", http_method="POST",
        http_path="/markets/indicator/custom/delete", http_tags=["markets"], memory="on",
        description="Delete a custom indicator. Input: id (str!). Output: {ok}.",
    )
    async def cap_custom_ind_delete(id: str = "", trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()

        def _del():
            conn = _sqlite_conn()
            try:
                conn.execute("DELETE FROM mkt_custom_indicators WHERE id=?", (id,))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _del)
        await emit_event({"type": "markets.indicators", "stage": "custom", "id": id,
                          "removed": True})
        return {"ok": True, "id": id}

    @capability(
        "markets.indicator.custom.test", http_method="POST",
        http_path="/markets/indicator/custom/test", http_tags=["markets"],
        memory="off", silent=True,
        description="Dry-run a custom-indicator expression against real stored bars "
                    "without saving. Input: expr (str) OR series (object), "
                    "dataset_id (str!), limit (int=300), full (bool=False — also "
                    "return the whole aligned series + timestamps for a live chart "
                    "preview). Output: {ok, bars, tail:{name:[last 10 values]}, "
                    "t?:[unix_sec], series?:{name:[values]}} or {error}.",
    )
    async def cap_custom_ind_test(expr: str = "", series=None, dataset_id: str = "",
                                  limit: int = 300, full: bool = False,
                                  trace_id=None) -> dict:
        if not dataset_id:
            return {"error": "dataset_id required"}
        if isinstance(series, str):
            try:
                series = json.loads(series)
            except Exception:
                series = None
        if not isinstance(series, dict) or not series:
            if not (expr or "").strip():
                return {"error": "expr or series required"}
            series = {"value": expr.strip()}
        arr, err = await _load_arrays(dataset_id, max(60, min(5000, int(limit))))
        if err:
            return err
        try:
            out = eval_custom_series(arr, series)
        except SafeExprError as e:
            return {"error": str(e)}
        res = {"ok": True, "bars": int(len(arr["t"])),
               "tail": {k: [None if math.isnan(x) else round(float(x), 6)
                            for x in v[-10:]] for k, v in out.items()}}
        if full:
            res["t"] = [int(x) for x in arr["t"]]
            res["series"] = {k: [None if math.isnan(x) else round(float(x), 6)
                                 for x in v] for k, v in out.items()}
        return res

    # ── Annotations (chart drawings & key dates) ─────────────────────────────

    def _ann_rows_sync(symbol_key: str, timeframe: str = "") -> List[dict]:
        conn = _sqlite_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM mkt_annotations WHERE symbol_key=? ORDER BY created_at",
                (symbol_key,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                if timeframe and d.get("timeframe") and d["timeframe"] != timeframe:
                    continue
                for f in ("points", "meta"):
                    try:
                        d[f] = json.loads(d[f]) if d.get(f) else ([] if f == "points" else {})
                    except Exception:
                        d[f] = [] if f == "points" else {}
                out.append(d)
            return out
        finally:
            conn.close()

    @capability(
        "markets.annotate.add", http_method="POST", http_path="/markets/annotate/add",
        http_tags=["markets"], memory="on",
        description="Draw on an asset's chart — persisted and pushed live to any open "
                    "Markets panel (this is how the agent draws for the user). "
                    "Input: symbol_key (str! — 'provider:symbol'), kind (str! — "
                    "trendline|ray|hline|vline|rect|fib|label|arrow; vline doubles as a "
                    "key-date marker), points (list! of {t:unix_sec, p:price} — hline "
                    "needs only p, vline only t, trendline/rect/fib need two points), "
                    "text (str — label shown on the chart), color (str — css color, "
                    "default by author), timeframe (str — ''=all timeframes), "
                    "author (str — 'vera' when the agent draws, 'user' from the UI). "
                    "Output: {ok, id, annotation}.",
    )
    async def cap_annotate_add(symbol_key: str = "", kind: str = "", points=None,
                               text: str = "", color: str = "", timeframe: str = "",
                               author: str = "vera", meta=None, trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        kind = (kind or "").lower().strip()
        if kind not in ANNOTATION_KINDS:
            return {"error": f"kind must be one of {sorted(ANNOTATION_KINDS)}"}
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except Exception:
                return {"error": "points must be a JSON list"}
        pts = points if isinstance(points, list) else ([points] if isinstance(points, dict) else [])
        norm = []
        for p in pts[:12]:
            if not isinstance(p, dict):
                continue
            q = {}
            if p.get("t") is not None:
                tt = float(p["t"])
                q["t"] = int(tt / 1000) if tt > 1e12 else int(tt)
            if p.get("p") is not None:
                q["p"] = float(p["p"])
            if q:
                norm.append(q)
        need_two = kind in ("trendline", "ray", "rect", "fib")
        if kind == "hline" and not any("p" in p for p in norm):
            return {"error": "hline needs points:[{p:price}]"}
        if kind == "vline" and not any("t" in p for p in norm):
            return {"error": "vline needs points:[{t:unix_sec}]"}
        if need_two and len(norm) < 2:
            return {"error": f"{kind} needs two points [{{t,p}},{{t,p}}]"}
        if kind in ("label", "arrow") and not norm:
            return {"error": f"{kind} needs one point {{t,p}}"}

        await _ensure_tables()
        aid = uuid.uuid4().hex[:10]
        row = {"id": aid, "symbol_key": symbol_key, "timeframe": timeframe or "",
               "kind": kind, "points": norm, "text": text or "",
               "color": color or ("#e8b34d" if author == "vera" else "#7aa2f7"),
               "author": author or "vera", "meta": meta if isinstance(meta, dict) else {},
               "created_at": now_iso(), "updated_at": now_iso()}

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO mkt_annotations "
                    "(id,symbol_key,timeframe,kind,points,text,color,author,meta,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, symbol_key, row["timeframe"], kind, json.dumps(norm),
                     row["text"], row["color"], row["author"], json.dumps(row["meta"]),
                     row["created_at"], row["updated_at"]))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.annotate", "stage": "added", "id": aid,
                          "symbol_key": symbol_key, "kind": kind, "author": row["author"],
                          "text": (text or "")[:120]})
        return {"ok": True, "id": aid, "annotation": row}

    @capability(
        "markets.annotate.list", http_method="GET", http_path="/markets/annotate/list",
        http_tags=["markets"], memory="off", silent=True,
        description="List chart annotations for an asset. Input: symbol_key (str!), "
                    "timeframe (str — filter; ''=all). Output: {annotations:[{id,kind,"
                    "points,text,color,author,timeframe,created_at}]}.",
    )
    async def cap_annotate_list(symbol_key: str = "", timeframe: str = "", trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _ann_rows_sync, symbol_key, timeframe)
        return {"symbol_key": symbol_key, "annotations": rows, "count": len(rows)}

    @capability(
        "markets.annotate.update", http_method="POST", http_path="/markets/annotate/update",
        http_tags=["markets"], memory="on",
        description="Move/edit an annotation. Input: id (str!), points (list), text (str), "
                    "color (str). Output: {ok, id}.",
    )
    async def cap_annotate_update(id: str = "", points=None, text: str = None,
                                  color: str = None, trace_id=None) -> dict:
        if not id:
            return {"error": "id required"}
        await _ensure_tables()
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except Exception:
                return {"error": "points must be a JSON list"}
        loop = asyncio.get_running_loop()

        def _upd():
            conn = _sqlite_conn()
            try:
                r = conn.execute("SELECT symbol_key FROM mkt_annotations WHERE id=?",
                                 (id,)).fetchone()
                if not r:
                    return None
                sets, args = ["updated_at=?"], [now_iso()]
                if isinstance(points, list):
                    sets.append("points=?"); args.append(json.dumps(points))
                if text is not None:
                    sets.append("text=?"); args.append(str(text))
                if color is not None:
                    sets.append("color=?"); args.append(str(color))
                args.append(id)
                conn.execute(f"UPDATE mkt_annotations SET {', '.join(sets)} WHERE id=?", args)
                conn.commit()
                return r[0]
            finally:
                conn.close()
        sym = await loop.run_in_executor(None, _upd)
        if sym is None:
            return {"error": "no such annotation", "id": id}
        await emit_event({"type": "markets.annotate", "stage": "updated",
                          "id": id, "symbol_key": sym})
        return {"ok": True, "id": id}

    @capability(
        "markets.annotate.remove", http_method="POST", http_path="/markets/annotate/remove",
        http_tags=["markets"], memory="on",
        description="Delete annotation(s). Input: id (str — one), or symbol_key (str) + "
                    "author (str — e.g. clear only 'vera' drawings) to bulk-clear. "
                    "Output: {ok, removed}.",
    )
    async def cap_annotate_remove(id: str = "", symbol_key: str = "",
                                  author: str = "", trace_id=None) -> dict:
        if not id and not symbol_key:
            return {"error": "id or symbol_key required"}
        await _ensure_tables()
        loop = asyncio.get_running_loop()

        def _del():
            conn = _sqlite_conn()
            try:
                if id:
                    r = conn.execute("SELECT symbol_key FROM mkt_annotations WHERE id=?",
                                     (id,)).fetchone()
                    conn.execute("DELETE FROM mkt_annotations WHERE id=?", (id,))
                    conn.commit()
                    return (r[0] if r else ""), conn.total_changes
                if author:
                    cur = conn.execute(
                        "DELETE FROM mkt_annotations WHERE symbol_key=? AND author=?",
                        (symbol_key, author))
                else:
                    cur = conn.execute(
                        "DELETE FROM mkt_annotations WHERE symbol_key=?", (symbol_key,))
                conn.commit()
                return symbol_key, cur.rowcount
            finally:
                conn.close()
        sym, n = await loop.run_in_executor(None, _del)
        await emit_event({"type": "markets.annotate", "stage": "removed",
                          "id": id, "symbol_key": sym, "removed": n})
        return {"ok": True, "removed": n}

    # ── Sentiment ─────────────────────────────────────────────────────────────

    def _sent_latest_sync(keys: List[str]) -> Dict[str, dict]:
        conn = _sqlite_conn()
        try:
            out = {}
            for k in keys:
                r = conn.execute(
                    "SELECT * FROM mkt_sentiment WHERE key=? ORDER BY created_at DESC LIMIT 1",
                    (k,)).fetchone()
                if r:
                    d = dict(r)
                    try:
                        d["headlines"] = json.loads(d.get("headlines") or "[]")
                    except Exception:
                        d["headlines"] = []
                    out[k] = d
            return out
        finally:
            conn.close()

    async def _analyze_sentiment(key: str, name: str = "", query: str = "",
                                 limit: int = 8) -> dict:
        """Search headlines → LLM-score → store. Returns the stored row dict."""
        await _ensure_tables()
        q = (query or "").strip() or sentiment_query_for(key, name)
        headlines = []
        web = sys.modules.get("web_capabilities")
        if web and hasattr(web, "cap_web_search"):
            try:
                r = await web.cap_web_search(query=q, limit=max(4, min(15, int(limit))),
                                             discover="off")
                for it in (r or {}).get("results", []):
                    t = (it.get("title") or "").strip()
                    s = (it.get("snippet") or "").strip()
                    if t:
                        headlines.append({"title": t[:200], "snippet": s[:280],
                                          "url": it.get("url", "")})
            except Exception as e:
                log.debug("sentiment search %s: %s", key, e)
        if not headlines:
            return {"error": f"no headlines found for '{q}' — web search unavailable or empty"}

        blob = "\n".join(f"- {h['title']}: {h['snippet']}" for h in headlines[:12])
        prompt = (
            f"You are a market sentiment analyst. Asset: {name or key}.\n"
            f"Recent headlines/snippets:\n{blob}\n\n"
            "Score the CURRENT market sentiment for this asset from these headlines.\n"
            "Reply with ONLY a JSON object:\n"
            '{"score": <float -1.0 (very bearish) to 1.0 (very bullish)>, '
            '"confidence": <float 0-1>, "label": "bearish|neutral|bullish", '
            '"summary": "<2 sentence summary of why>", '
            '"drivers": ["<short driver>", ...]}'
        )
        try:
            raw = await ollama_generate(prompt, json_mode=True, job_type="analysis",
                                        timeout=120)
        except Exception as e:
            return {"error": f"LLM scoring failed: {e}"}
        parsed = parse_llm_json(raw) or {}
        try:
            score = max(-1.0, min(1.0, float(parsed.get("score", 0))))
        except Exception:
            score = 0.0
        try:
            conf = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
        except Exception:
            conf = 0.5
        label = str(parsed.get("label") or
                    ("bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"))
        summary = str(parsed.get("summary") or "")[:600]
        row = {"id": uuid.uuid4().hex[:10], "key": key, "name": name or key,
               "score": score, "confidence": conf, "label": label,
               "summary": summary, "headlines": headlines[:10], "created_at": now_iso()}

        def _ins():
            conn = _sqlite_conn()
            try:
                conn.execute(
                    "INSERT INTO mkt_sentiment (id,key,name,score,confidence,label,summary,"
                    "headlines,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["id"], key, row["name"], score, conf, label, summary,
                     json.dumps(headlines[:10]), row["created_at"]))
                conn.commit()
            finally:
                conn.close()
        await asyncio.get_running_loop().run_in_executor(None, _ins)
        await emit_event({"type": "markets.sentiment", "stage": "scored", "key": key,
                          "score": score, "label": label})
        return row

    @capability(
        "markets.sentiment.analyze", http_method="POST",
        http_path="/markets/sentiment/analyze", http_tags=["markets"], memory="on",
        description="Score current market sentiment for one asset: fetches fresh headlines "
                    "(web.search) and has the LLM rate them -1(bearish)…+1(bullish); the "
                    "snapshot is stored as a time series. Input: symbol_key (str! — "
                    "'provider:symbol' or a key-asset key like 'yahoo:^GSPC'), name (str — "
                    "display name to improve the search), query (str — override the search "
                    "query). Output: {key,score,confidence,label,summary,headlines}.",
    )
    async def cap_sentiment_analyze(symbol_key: str = "", name: str = "",
                                    query: str = "", trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        return await _analyze_sentiment(symbol_key, name, query)

    @capability(
        "markets.sentiment.map", http_method="GET", http_path="/markets/sentiment/map",
        http_tags=["markets"], memory="off", silent=True,
        description="Latest sentiment for every tracked asset plus the key market "
                    "benchmarks (S&P 500, Nasdaq, VIX, Gold, Oil, DXY, BTC, ETH) — feeds "
                    "the sentiment heat-map. Input: none. Output: {tracked:[{key,name,"
                    "score,label,confidence,summary,age_min}], benchmarks:[...]}.",
    )
    async def cap_sentiment_map(trace_id=None) -> dict:
        await _ensure_tables()
        mc = _mc()
        loop = asyncio.get_running_loop()
        tracked_keys = []
        if mc:
            try:
                rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
                tracked_keys = [(r["id"], r["symbol"]) for r in rows]
            except Exception:
                pass
        bench_keys = [(k["key"], k["name"]) for k in KEY_MARKET_ASSETS]
        latest = await loop.run_in_executor(
            None, _sent_latest_sync, [k for k, _ in tracked_keys + bench_keys])

        def _fmt(key, nm):
            d = latest.get(key)
            if not d:
                return {"key": key, "name": nm, "score": None, "label": "unscored"}
            age = None
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(str(d["created_at"]).replace("Z", ""))
                age = int((time.time() - ts.timestamp()) / 60)
            except Exception:
                pass
            return {"key": key, "name": nm, "score": d.get("score"),
                    "label": d.get("label"), "confidence": d.get("confidence"),
                    "summary": d.get("summary"), "age_min": age,
                    "created_at": d.get("created_at")}
        return {"tracked": [_fmt(k, n) for k, n in tracked_keys],
                "benchmarks": [_fmt(k, n) for k, n in bench_keys]}

    @capability(
        "markets.sentiment.refresh", http_method="POST",
        http_path="/markets/sentiment/refresh", http_tags=["markets"], memory="on",
        description="Refresh stale sentiment scores in the background (sequential, "
                    "rate-friendly). Input: scope (str — all|tracked|benchmarks, "
                    "default all), max_age_min (int=240 — skip assets scored more "
                    "recently). Output: {ok, queued:[keys]}.",
    )
    async def cap_sentiment_refresh(scope: str = "all", max_age_min: int = 240,
                                    trace_id=None) -> dict:
        await _ensure_tables()
        mc = _mc()
        loop = asyncio.get_running_loop()
        targets = []
        if scope in ("all", "tracked") and mc:
            try:
                rows = await loop.run_in_executor(None, mc._watchlist_rows_sync)
                targets += [(r["id"], r["symbol"]) for r in rows]
            except Exception:
                pass
        if scope in ("all", "benchmarks"):
            targets += [(k["key"], k["name"]) for k in KEY_MARKET_ASSETS]
        latest = await loop.run_in_executor(None, _sent_latest_sync, [k for k, _ in targets])
        cutoff = time.time() - max(5, int(max_age_min)) * 60
        queued = []
        for key, nm in targets:
            d = latest.get(key)
            if d:
                try:
                    from datetime import datetime as _dt
                    ts = _dt.fromisoformat(str(d["created_at"]).replace("Z", ""))
                    if ts.timestamp() > cutoff:
                        continue
                except Exception:
                    pass
            queued.append((key, nm))

        async def _worker(items):
            for key, nm in items:
                try:
                    await _analyze_sentiment(key, nm)
                except Exception as e:
                    log.debug("sentiment refresh %s: %s", key, e)
                await asyncio.sleep(2.0)
            await emit_event({"type": "markets.sentiment", "stage": "refresh_done",
                              "count": len(items)})
        if queued:
            asyncio.create_task(_worker(list(queued)))
        return {"ok": True, "queued": [k for k, _ in queued], "count": len(queued)}

    @capability(
        "markets.sentiment.history", http_method="GET",
        http_path="/markets/sentiment/history", http_tags=["markets"],
        memory="off", silent=True,
        description="Sentiment score time series for one asset. Input: symbol_key (str!), "
                    "limit (int=100). Output: {history:[{score,label,confidence,summary,"
                    "created_at}]}.",
    )
    async def cap_sentiment_history(symbol_key: str = "", limit: int = 100,
                                    trace_id=None) -> dict:
        if not symbol_key:
            return {"error": "symbol_key required"}
        await _ensure_tables()

        def _rows():
            conn = _sqlite_conn()
            try:
                rows = conn.execute(
                    "SELECT score,label,confidence,summary,created_at FROM mkt_sentiment "
                    "WHERE key=? ORDER BY created_at DESC LIMIT ?",
                    (symbol_key, max(1, min(1000, int(limit))))).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        rows = await asyncio.get_running_loop().run_in_executor(None, _rows)
        rows.reverse()
        return {"symbol_key": symbol_key, "history": rows, "count": len(rows)}

    log.info("markets analysis capabilities loaded (numpy=%s)", HAS_NUMPY)

elif _CAP_AVAILABLE and not HAS_NUMPY:          # pragma: no cover
    log.warning("markets analysis disabled — numpy not installed")
