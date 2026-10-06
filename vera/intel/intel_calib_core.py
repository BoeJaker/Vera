"""Intelligence - a calibrated relevance gate per topic (pure).

The relevance gate today is a fixed score threshold. Once the reader has rated
enough signals (useful / not useful / mute, links opened, report buttons), a
topic's own curve can say how likely a signal at a given score is to be worth
it: p(useful | score) = 1 / (1 + exp(-(a*score + b))), fitted by Newton's method
on the logistic log-likelihood with a small ridge so a handful of labels cannot
produce an absurd slope.

Honesty rules (four-minds ROADMAP §7, FM-6.3 "per-topic calibrated gate"):
  - a curve is only *used* when it is calibrated - expected calibration error
    (ECE, 10 equal-width bins) at most 0.05 - and it was fitted on enough
    labels of both kinds (default 30, at least 8 of the rarer class);
  - until then it runs in shadow: decisions carry the shadow p in detail, the
    gate keeps the fixed threshold, and the decision's own `p` stays null;
  - the curve and its numbers are always visible (intel.calibration.status).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

POSITIVE = {"user.feedback.useful", "link.opened", "report.button", "alert.acted"}
NEGATIVE = {"user.feedback.not_useful", "user.feedback.mute_site", "alert.ignored"}
MIN_LABELS = 30
MIN_MINORITY = 8
MAX_ECE = 0.05


def label_of(sources: Iterable[str]) -> Optional[int]:
    """1 useful, 0 not useful, None unknown - from the outcome sources a signal
    collected. An explicit rating beats an implicit one; a link opened then
    rated not useful is not useful."""
    s = set(sources or [])
    if s & {"user.feedback.not_useful", "user.feedback.mute_site"}:
        return 0
    if "user.feedback.useful" in s:
        return 1
    if s & POSITIVE:
        return 1
    if s & NEGATIVE:
        return 0
    return None


def _sig(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit(xs: Sequence[float], ys: Sequence[int], *, ridge: float = 0.01, iters: int = 50) -> Tuple[float, float]:
    """(a, b) of p = sigmoid(a*x + b), Newton-Raphson with an L2 ridge on a."""
    a, b = 0.0, 0.0
    for _ in range(iters):
        ga = gb = 0.0
        haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            p = _sig(a * x + b)
            w = p * (1 - p)
            ga += (p - y) * x
            gb += (p - y)
            haa += w * x * x
            hab += w * x
            hbb += w
        ga += ridge * a
        haa += ridge
        hbb += 1e-9
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    return a, b


def ece(ps: Sequence[float], ys: Sequence[int], bins: int = 10) -> float:
    """Expected calibration error: the weighted gap between predicted and seen
    rates over equal-width probability bins."""
    if not ps:
        return 1.0
    tot = 0.0
    for k in range(bins):
        lo, hi = k / bins, (k + 1) / bins
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (k == bins - 1 and p == 1.0)]
        if idx:
            conf = sum(ps[i] for i in idx) / len(idx)
            acc = sum(ys[i] for i in idx) / len(idx)
            tot += len(idx) / len(ps) * abs(conf - acc)
    return round(tot, 4)


def auc(ps: Sequence[float], ys: Sequence[int]) -> float:
    """Probability that a useful signal outranks a not-useful one (ties half)."""
    pos = [p for p, y in zip(ps, ys) if y]
    neg = [p for p, y in zip(ps, ys) if not y]
    if not pos or not neg:
        return 0.5
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return round(wins / (len(pos) * len(neg)), 4)


MIN_AUC = 0.6


def calibrate(pairs: Sequence[Tuple[float, int]], *, min_labels: int = MIN_LABELS,
              min_minority: int = MIN_MINORITY, max_ece: float = MAX_ECE) -> Dict[str, Any]:
    """A topic's curve from (score, label) pairs, with its honesty numbers and
    whether it may be used. Calibration is measured on held-out folds (5-fold),
    not on the points it was fitted to."""
    xs = [float(x) for x, _ in pairs]
    ys = [int(y) for _, y in pairs]
    n, pos = len(ys), sum(ys)
    out: Dict[str, Any] = {"n": n, "useful": pos, "not_useful": n - pos, "usable": False}
    if n < min_labels or min(pos, n - pos) < min_minority:
        out["why"] = f"needs {min_labels} rated signals with at least {min_minority} of each kind (has {pos} useful, {n - pos} not)"
        return out
    a, b = fit(xs, ys)
    folds = 5
    held_p: List[float] = []
    held_y: List[int] = []
    for f in range(folds):
        tr = [i for i in range(n) if i % folds != f]
        te = [i for i in range(n) if i % folds == f]
        fa, fb = fit([xs[i] for i in tr], [ys[i] for i in tr])
        held_p += [_sig(fa * xs[i] + fb) for i in te]
        held_y += [ys[i] for i in te]
    e = ece(held_p, held_y)
    disc = auc(held_p, held_y)
    cut = (-b / a) if a > 1e-6 else None                 # the score where p = 0.5
    # calibrated is not enough: a flat curve (p ~ base rate everywhere) is perfectly
    # calibrated and useless, so the held-out curve must also tell the two apart
    out.update(a=round(a, 4), b=round(b, 4), ece=e, auc=disc, score_at_half=round(cut, 2) if cut is not None else None,
               usable=bool(e <= max_ece and a > 0 and disc >= MIN_AUC))
    if not out["usable"]:
        out["why"] = (f"calibration error {e:.3f} is above {max_ece}" if e > max_ece
                      else f"the score barely separates useful from not (held-out AUC {disc:.2f} < {MIN_AUC})"
                      if disc < MIN_AUC else "the curve does not rise with the score")
    return out


def p_useful(curve: Optional[Dict[str, Any]], score: Optional[float]) -> Optional[float]:
    if not curve or curve.get("a") is None or score is None:
        return None
    return round(_sig(float(curve["a"]) * float(score) + float(curve["b"])), 4)
