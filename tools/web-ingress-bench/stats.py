"""Web-ingress bench statistics (WI-0): summaries of timing samples, a two-sample Mann-Whitney U test, and the
comparison of a run against a baseline under the plan's budgets (shared-planning web-ingress PLAN §6). Pure."""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

#: PLAN §6. Per workload: the allowed regression of p50 and p95 against the baseline, as a fraction (0.05 = +5 %),
#: or an absolute ceiling in ms. A workload not listed is reported but never fails the comparison.
BUDGETS: Dict[str, Dict[str, float]] = {
    "fixture.web_fetch":   {"p50_rel": 0.05, "p95_rel": 0.10},
    "live.web_fetch":      {"mw_p": 0.05},        # no significant difference (Mann-Whitney, two-sided)
    "live.web_search":     {"mw_p": 0.05},
    "live.recall":         {"p50_abs": 200.0, "p95_abs": 400.0},
    "micro.enqueue":       {"p99_abs": 1.0},       # ms
}


def percentile(xs: Iterable[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in 0..100); None for no samples."""
    v = sorted(float(x) for x in xs)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q / 100.0
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def summarise(xs: Iterable[float]) -> Dict[str, Optional[float]]:
    v = [float(x) for x in xs]
    return {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "p99": percentile(v, 99),
            "max": max(v) if v else None, "min": min(v) if v else None}


def mann_whitney(a: List[float], b: List[float]) -> Dict[str, Optional[float]]:
    """Two-sided Mann-Whitney U with the normal approximation and tie correction. p is None below 3 samples a side
    (too few to say anything)."""
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return {"u": None, "z": None, "p": None}
    pooled = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = [0.0] * len(pooled)
    i, ties = 0, 0.0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u = min(u1, n1 * n2 - u1)
    n = n1 + n2
    sd = math.sqrt(n1 * n2 / 12.0 * ((n + 1) - ties / (n * (n - 1))))
    if sd == 0:
        return {"u": u, "z": 0.0, "p": 1.0}
    z = (u - n1 * n2 / 2.0) / sd
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return {"u": u, "z": z, "p": min(1.0, p)}


def compare(base: Dict[str, List[float]], run: Dict[str, List[float]],
            budgets: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Dict[str, object]]:
    """Per workload in the run: the summaries, the deltas against the baseline, and whether the budget holds
    (ok None = no budget or not enough data to judge)."""
    budgets = BUDGETS if budgets is None else budgets
    out: Dict[str, Dict[str, object]] = {}
    for name, xs in sorted(run.items()):
        s, b = summarise(xs), summarise(base.get(name) or [])
        row: Dict[str, object] = {"run": s, "base": b, "ok": None, "why": []}
        bud = budgets.get(name) or {}
        why: List[str] = []
        ok: Optional[bool] = None if not bud else True
        for key, lim in bud.items():
            stat, kind = key.rsplit("_", 1)
            if kind == "abs":
                val = s.get(stat)
                if val is None:
                    ok = None if ok is not False else ok
                    continue
                if val > lim:
                    ok = False
                    why.append(f"{stat} {val:.1f} ms > {lim:.1f} ms")
            elif kind == "rel":
                val, ref = s.get(stat), b.get(stat)
                if val is None or not ref:
                    ok = None if ok is not False else ok
                    continue
                d = val / ref - 1.0
                row[f"{stat}_delta"] = d
                if d > lim:
                    ok = False
                    why.append(f"{stat} +{d * 100:.1f} % > +{lim * 100:.0f} %")
            elif key == "mw_p":
                mw = mann_whitney(list(base.get(name) or []), list(xs))
                row["mann_whitney"] = mw
                if mw["p"] is None:
                    ok = None if ok is not False else ok
                elif mw["p"] < lim and (s["p50"] or 0) > (b["p50"] or 0):
                    ok = False
                    why.append(f"slower and significant (p={mw['p']:.3f})")
        row["ok"], row["why"] = ok, why
        out[name] = row
    return out


def verdict(cmp: Dict[str, Dict[str, object]]) -> str:
    """'fail' if any budget broke, 'pass' if every budgeted workload held, 'inconclusive' otherwise."""
    oks = [r["ok"] for r in cmp.values() if r["ok"] is not None]
    if any(o is False for o in oks):
        return "fail"
    budgeted = [n for n in cmp if n in BUDGETS]
    return "pass" if oks and len(oks) == len(budgeted) else "inconclusive"
