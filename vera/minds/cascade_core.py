"""Which student may answer each decision question first, and from what confidence (four-minds FM-2.5, every
question at once - owner, 17 Oct: "set up all the evaluations and training in parallel").

The nightly shadow judges score every implementation on every question against the decisions' outcomes
(decide.shadow.report: accuracy, and precision/coverage at confidence 0.5/0.7/0.8/0.9). From that, for each
question:

- only an implementation the loop can run live counts (`runnable`: the kNN judge, the in-slot student LLM, a
  trained student once one is bound);
- it is eligible at the LOWEST confidence threshold where its precision on outcomes is at least the live model's
  own accuracy on the same outcomes, with at least MIN_N outcomes for both and at least MIN_COVERAGE of decisions
  above the threshold;
- of the eligible ones the widest coverage wins (kNN first on a tie: milliseconds, no LLM).

A question with no eligible implementation keeps the big model; its best runnable candidate is still named, for
shadow. The combined census arm turns `first` on for every eligible question at once; per-decision accuracy stays
separable because every decision records who decided and its outcome.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

TAUS = ("0.5", "0.7", "0.8", "0.9")
MIN_N = 30
MIN_COVERAGE = 0.2
KNN = "knn:minilm"


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def runnable(impl: str, student_model: str, extra: Iterable[str] = ()) -> bool:
    """Can the loop run this implementation live? kNN, the in-slot student LLM (no @node: the live student is
    routed, not pinned to a CPU node), or a named extra (a bound trained student)."""
    return impl == KNN or impl == f"llm:{student_model}" or impl in set(extra)


def candidate(impl: str, row: Mapping[str, Any], live_acc: float) -> Optional[Dict[str, Any]]:
    """This implementation's first eligible threshold, or None."""
    vo = row.get("vs_outcomes") or {}
    if int(vo.get("n") or 0) < MIN_N:
        return None
    at = vo.get("at_confidence") or {}
    for t in TAUS:
        c = at.get(t) or {}
        prec, cov = _num(c.get("precision")), _num(c.get("coverage"))
        if prec is None or cov is None:
            continue
        if prec >= live_acc and cov >= MIN_COVERAGE:
            return {"impl": impl, "tau": float(t), "precision": prec, "coverage": cov, "outcomes_n": int(vo["n"])}
    return None


def choose(report: Mapping[str, Any], student_model: str, extra: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    """{question: {eligible, impl, tau, precision, coverage, outcomes_n, live_accuracy, live_n, reason}}."""
    extra = list(extra)
    live = report.get("live") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for q, impls in (report.get("questions") or {}).items():
        lv = live.get(q) or {}
        live_n, live_acc = int(lv.get("n") or 0), _num(lv.get("accuracy"))
        runs = {i: r for i, r in (impls or {}).items() if runnable(i, student_model, extra)}
        base = {"eligible": False, "impl": "", "tau": None, "live_accuracy": live_acc, "live_n": live_n}
        if live_acc is None or live_n < MIN_N:
            out[q] = dict(base, reason=f"too few live outcomes ({live_n} < {MIN_N})")
            continue
        cands = [c for c in (candidate(i, r, live_acc) for i, r in runs.items()) if c]
        if cands:
            best = max(cands, key=lambda c: (c["coverage"], c["impl"] == KNN, -c["tau"]))
            out[q] = dict(base, eligible=True, **best,
                          reason=f"{best['impl']} at >= {best['tau']}: precision {best['precision']} >= live "
                                 f"{live_acc} on {best['coverage']:.0%} of decisions")
            continue
        # not eligible: name the runnable implementation with the most outcomes, for shadow
        ranked = sorted(runs.items(), key=lambda kv: -int(((kv[1] or {}).get("vs_outcomes") or {}).get("n") or 0))
        out[q] = dict(base, impl=ranked[0][0] if ranked else "",
                      reason="no runnable student reaches the live model's accuracy with enough outcomes and coverage")
    return out


def arm_minds(plan: Mapping[str, Mapping[str, Any]], features: Mapping[str, str]) -> Dict[str, str]:
    """The combined census arm: `first` for every eligible question that has a switch."""
    return {features[q]: "first" for q, c in plan.items() if c.get("eligible") and q in features}
