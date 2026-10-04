"""Intelligence - change detection on numeric series (pure, no numpy).

An Indicator (a price, a macro series, a prediction-market probability, a daily
event count) is only interesting when it CHANGES. Three detectors, each cheap and
explainable, each returning the same `Change` shape so an assessment can say "X moved
2.4 sigma on 3 Oct, after these stories":

- spikes:   a robust z-score of one-bar returns (median / MAD, so a single outlier
            in the history does not hide the next one);
- shifts:   a two-sided CUSUM on standardised values (a level that moved and stayed);
- regimes:  Bayesian online change-point detection (Adams & MacKay 2007) with a
            constant hazard and a Gaussian model with unknown mean (known variance
            from a robust estimate) - the run-length posterior collapsing means "a new
            regime started here".

Nothing here fetches data; the caller supplies (t, value) points.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mad_sigma(xs: Sequence[float]) -> float:
    """A robust standard deviation: 1.4826 × median absolute deviation."""
    if len(xs) < 2:
        return 0.0
    m = _median(xs)
    return 1.4826 * _median([abs(x - m) for x in xs])


def returns(values: Sequence[float], kind: str = "pct") -> List[float]:
    """One-bar changes: percentage for prices, absolute for probabilities / rates."""
    out = []
    for a, b in zip(values, values[1:]):
        if kind == "pct":
            out.append((b - a) / a if a else 0.0)
        else:
            out.append(b - a)
    return out


def change(kind: str, i: int, t: Any, magnitude: float, direction: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": kind, "index": i, "t": t, "magnitude": round(float(magnitude), 3),
            "direction": direction, "detail": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in detail.items()}}


def spikes(ts: Sequence[Any], values: Sequence[float], *, window: int = 40, z: float = 5.0,
           kind: str = "pct", min_history: int = 20, min_sigma: float = 0.0) -> List[Dict[str, Any]]:
    """One-bar moves larger than `z` robust sigmas of the trailing window's moves.
    `min_sigma` floors the sigma, so a series that mostly sits still (a prediction
    market between trades) still reports its jumps."""
    r = returns(values, kind)
    out = []
    for i in range(len(r)):
        hist = r[max(0, i - window):i]
        if len(hist) < min_history:
            continue
        sig = max(mad_sigma(hist), min_sigma)
        if sig <= 0:
            continue
        zz = (r[i] - _median(hist)) / sig
        if abs(zz) >= z:
            out.append(change("spike", i + 1, ts[i + 1], abs(zz), "up" if zz > 0 else "down",
                              {"z": zz, "move": r[i]}))
    return out


def cusum(ts: Sequence[Any], values: Sequence[float], *, warmup: int = 30, k: float = 0.5, h: float = 8.0
          ) -> List[Dict[str, Any]]:
    """Two-sided CUSUM on values standardised by a robust baseline. After an alarm it
    waits `warmup` bars and re-baselines on the NEW level's own data, so a sustained
    level shift is one change, not one per bar."""
    if len(values) <= warmup + 1:
        return []
    def base(seg):
        mu, sg = _median(seg), mad_sigma(seg)
        return mu, (sg or (abs(mu) * 0.01) or 1.0)
    mu, sig = base(list(values[:warmup]))
    pos = neg = 0.0
    out = []
    i = warmup
    while i < len(values):
        x = (values[i] - mu) / sig
        pos = max(0.0, pos + x - k)
        neg = max(0.0, neg - x - k)
        if pos > h or neg > h:
            up = pos > h
            out.append(change("shift", i, ts[i], max(pos, neg), "up" if up else "down",
                              {"level_sigma": x, "baseline": mu}))
            nxt = min(len(values), i + warmup)
            if nxt - i < 5:
                break
            mu, sig = base(list(values[i:nxt]))
            pos = neg = 0.0
            i = nxt
            continue
        i += 1
    return out


def bocpd(ts: Sequence[Any], values: Sequence[float], *, hazard: float = 1 / 100.0, threshold: float = 0.5,
          max_run: int = 300, min_gap: int = 5) -> List[Dict[str, Any]]:
    """Bayesian online change-point detection. Gaussian observations with unknown
    mean (Normal prior) and a variance fixed from a robust estimate of the whole
    series. A change point is reported where the posterior probability that the run
    length is short (≤ 3) first exceeds `threshold`."""
    n = len(values)
    if n < 8:
        return []
    var = (mad_sigma(returns(values, "abs")) / math.sqrt(2)) ** 2 or (mad_sigma(values) ** 2) or 1.0
    mu0, kappa0 = values[0], 1.0
    # run-length distribution and sufficient statistics per run length
    R = [1.0]
    mus, kappas = [mu0], [kappa0]
    out: List[Dict[str, Any]] = []
    last = -min_gap
    for i in range(1, n):
        x = values[i]
        preds = []
        for m, kap in zip(mus, kappas):
            s2 = var * (1 + 1 / kap)
            preds.append(math.exp(-0.5 * (x - m) ** 2 / s2) / math.sqrt(2 * math.pi * s2))
        growth = [r * p * (1 - hazard) for r, p in zip(R, preds)]
        cp = sum(r * p * hazard for r, p in zip(R, preds))
        newR = [cp] + growth
        z = sum(newR) or 1e-300
        R = [r / z for r in newR][:max_run]
        mus = [mu0] + [(kap * m + x) / (kap + 1) for m, kap in zip(mus, kappas)]
        kappas = [kappa0] + [kap + 1 for kap in kappas]
        mus, kappas = mus[:max_run], kappas[:max_run]
        mu0 = x                            # a new run starts at the current level
        p_short = sum(R[:4])
        if i > 4 and p_short > threshold and i - last >= min_gap:
            before = values[max(0, i - 10):i]
            direction = "up" if x > _median(before) else "down"
            out.append(change("regime", i, ts[i], p_short, direction, {"p_new_regime": p_short}))
            last = i
    return out


def level_move(values: Sequence[float], i: int, window: int) -> float:
    """How far the level settled: the median of the `window` bars from i on, less
    the median of the `window` bars before i."""
    before, after = list(values[max(0, i - window):i]), list(values[i:i + window])
    if not before or not after:
        return 0.0
    return _median(after) - _median(before)


# A prediction market is quoted in whole points and sits still between trades, so
# its hourly series is all small steps: only a move that SETTLES at least this far
# (in probability) is a change, and moves within a day of each other are one change.
PROB_MIN_MOVE = 0.05
PROB_WINDOW = 24


def detect(ts: Sequence[Any], values: Sequence[float], *, kind: str = "price") -> List[Dict[str, Any]]:
    """All three detectors with defaults suited to the indicator kind, merged and
    de-duplicated (changes within 5 bars of each other - a day for a probability -
    are one change; the spike, then the shift, is kept). Each change carries the
    level it settled at (`detail.level_move`); a probability keeps only changes
    that settled at least PROB_MIN_MOVE away, in the direction they settled."""
    vals = [float(v) for v in values]
    prob = kind == "probability"
    rk = "abs" if kind in ("probability", "rate", "count", "geo_rate", "macro") else "pct"
    found = spikes(ts, vals, kind=rk, min_sigma=0.005 if prob else 0.0) + cusum(ts, vals) + bocpd(ts, vals)
    gap = PROB_WINDOW if prob else 5
    by_i: Dict[int, Dict[str, Any]] = {}
    pri = {"spike": 0, "shift": 1, "regime": 2}
    for c in sorted(found, key=lambda c: (c["index"], pri[c["kind"]])):
        if not any(abs(c["index"] - j) <= gap for j in by_i):
            by_i[c["index"]] = c
    out = []
    for k in sorted(by_i):
        c = by_i[k]
        lm = level_move(vals, c["index"], PROB_WINDOW if prob else 5)
        c["detail"]["level_move"] = round(lm, 4)
        if prob:
            if abs(lm) < PROB_MIN_MOVE:
                continue
            c["direction"] = "up" if lm > 0 else "down"
        out.append(c)
    return out


def corroborate(change_t: float, stories: Sequence[Dict[str, Any]], *, before_h: float = 24.0,
                after_h: float = 48.0) -> List[Dict[str, Any]]:
    """Stories whose first sighting falls in the window around a change (story times
    and the change time as epoch seconds), strongest first."""
    out = []
    for s in stories:
        st = s.get("t")
        if st is None:
            continue
        dt_h = (change_t - float(st)) / 3600.0
        if -after_h <= dt_h <= before_h:
            out.append(dict(s, lead_h=round(dt_h, 1)))
    return sorted(out, key=lambda s: (-(s.get("score") or 0), abs(s["lead_h"])))


# ── indicators: what a topic watches, and how a change reads ─────────────────

KINDS = ("price", "macro", "probability", "count")


def describe(c: Dict[str, Any], label: str, kind: str = "price") -> str:
    """One line a reader understands: "BTC/USDT up 8.1% on 2026-10-03 (spike, 6.2 sigma)"."""
    from datetime import datetime, timezone
    try:
        day = datetime.fromtimestamp(float(c.get("t")), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        day = str(c.get("t") or "")[:10]
    d = c.get("detail") or {}
    if c.get("kind") == "spike" and d.get("move") is not None:
        mv = float(d["move"])
        size = f"{abs(mv) * 100:.1f}%" if kind == "price" else (
            f"{abs(mv) * 100:.0f} points" if kind == "probability" else f"{abs(mv):.3g}")
        how = f"spike, {abs(float(d.get('z') or c.get('magnitude') or 0)):.1f} sigma"
    elif kind == "probability" and d.get("level_move") is not None:
        size = f"{abs(float(d['level_move'])) * 100:.0f} points"
        how = "and held" if c.get("kind") != "regime" else "into a new regime"
    elif c.get("kind") == "shift":
        size, how = "to a new level", "sustained shift"
    else:
        size, how = "into a new regime", f"regime change, p={float(c.get('magnitude') or 0):.2f}"
    return f"{label} {c.get('direction', '')} {size} on {day} ({how})".replace("  ", " ")


def material(changes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The moves worth an interruption: a spike or a shift that stories explain
    (corroborated), or any spike past 8 sigma on its own."""
    out = []
    for c in changes:
        if c.get("kind") == "regime":
            continue
        if c.get("stories") or (c.get("kind") == "spike" and float(c.get("magnitude") or 0) >= 8.0):
            out.append(c)
    return out


def prompt_lines(changes: Sequence[Dict[str, Any]], n: int = 6) -> str:
    """Indicator moves for an assessment prompt, each with the stories around it."""
    lines = []
    for c in list(changes)[-n:]:
        line = "- " + (c.get("text") or "")
        st = [s.get("title") for s in (c.get("stories") or [])[:2] if s.get("title")]
        if st:
            line += " | around then: " + "; ".join(st)
        lines.append(line[:400])
    return "\n".join(lines)
