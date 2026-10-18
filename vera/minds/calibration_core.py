"""decide.calibration: how every System 1 student is doing, per question and implementation (four-minds FM-11).

FM-2.5 promotes a student on precision at tau and ECE, and reverts it automatically on a rolling precision drop of
3 points, escalation above 60% or drift. This module computes those numbers for a window and the window before it,
from rows the capability reads out of the ledger:

per (question, implementation) - the implementation tag carries the node when the student was pinned to one
(`llm:<model>@<node>`), so calibration is per (question, package, node) as the plan asks:
  n, agreement with the live answer; against gold (export_core.settle): accuracy, ECE, precision and coverage at
  each confidence threshold; co-sign precision (the `sampled` share: answers that would have stood, decided by the
  big model instead - did the student agree?);
per question:
  escalation - in runs where a student decided (student_first), the share of model-reachable verdicts it handed
  to the big model; drop - live decisions on a stored surface whose inputs never reached the training store;
  orphans - shadows whose live decision is missing; drift - total-variation distance between the live answer mixes
  of the two windows;
alerts - the FM-2.5 revert triggers (and ECE above 0.05), each only once both windows have enough rows.

Pure: no I/O.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

try:
    from Vera.vera.minds import judge_core as JC, export_core as EX
except Exception:                       # worktree / app-free import
    from vera.minds import judge_core as JC, export_core as EX  # type: ignore

THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95)
MIN_N = 20                    # below this a window's number is shown but never alerts
PRECISION_DROP = 0.03         # FM-2.5: rolling precision -3 points
ESCALATION_MAX = 0.60         # FM-2.5: escalation > 60%
ECE_MAX = 0.05                # FM-2.5: ECE <= 0.05
DRIFT_MAX = 0.25              # total-variation distance between the windows' live answer mixes


def _calib(question_id: str, rows: List[Dict[str, Any]], tau: float) -> Dict[str, Any]:
    """Calibration of one implementation's answers against gold; precision at tau included."""
    gold = [(r["answer"], r.get("p"), r["gold"]) for r in rows if r.get("gold") is not None]
    pos = JC.positive_label(question_id)
    ths = tuple(sorted(set(THRESHOLDS) | {round(float(tau), 2)}))
    if pos is not None:
        c = JC.calibration([(p, g) for _, p, g in gold], pos, thresholds=ths)
    else:
        c = JC.choice_calibration(gold, thresholds=ths)
    c["precision_at_tau"] = ((c.get("at_confidence") or {}).get(str(round(float(tau), 2))) or {}).get("precision")
    return c


def implementation_stats(question_id: str, rows: List[Dict[str, Any]], tau: float = 0.95) -> Dict[str, Any]:
    """rows: one per shadow answer {answer, p, live, gold, would_take}."""
    n = len(rows)
    if not n:
        return {"n": 0}
    out: Dict[str, Any] = {"n": n, "agreement": round(sum(1 for r in rows if r["answer"] == r.get("live")) / n, 3)}
    out["vs_gold"] = _calib(question_id, rows, tau)
    cos = [r for r in rows if r.get("would_take")]
    if cos:
        out["cosign"] = {"n": len(cos), "agrees_with_live": round(sum(1 for r in cos if r["answer"] == r.get("live")) / len(cos), 3)}
        g = [r for r in cos if r.get("gold") is not None]
        if g:
            out["cosign"]["right_vs_gold"] = round(sum(1 for r in g if r["answer"] == r["gold"]) / len(g), 3)
    return out


def escalation(live: Iterable[Dict[str, Any]], cosigned: Iterable[str] = ()) -> Dict[str, Any]:
    """live: {question_id, run_id, decider, mode, decision_id}. In runs where a student decided under
    student_first, per question: took (the student's answer stood), handed (the big model decided; co-signed samples
    left out), rate = handed / both. A heuristic recorded as decider=student (intel) is not a cascade and does not
    count."""
    live = list(live)
    cos = set(cosigned)
    runs: Dict[str, set] = {}
    def _took(d):
        return d.get("decider") == "student" and d.get("mode", "student_first") == "student_first"
    for d in live:
        if _took(d):
            runs.setdefault(d["question_id"], set()).add(d.get("run_id"))
    out: Dict[str, Any] = {}
    for q, rs in runs.items():
        took = sum(1 for d in live if d["question_id"] == q and _took(d))
        handed = sum(1 for d in live if d["question_id"] == q and d.get("run_id") in rs and d.get("decider") == "llm"
                     and d.get("decision_id") not in cos)
        out[q] = {"took": took, "handed": handed, "rate": round(handed / (took + handed), 3) if took + handed else None}
    return out


def drift(cur: Iterable[Any], prev: Iterable[Any]) -> Optional[float]:
    """Total-variation distance between two answer mixes (None when either is empty)."""
    a, b = Counter(str(x) for x in cur), Counter(str(x) for x in prev)
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return None
    return round(0.5 * sum(abs(a[k] / na - b[k] / nb) for k in set(a) | set(b)), 3)


def gold_of(live_row: Dict[str, Any], outcomes: Iterable[Dict[str, Any]]) -> Optional[str]:
    s = EX.settle(live_row or {}, outcomes or [])
    return s.get("label") if s else None


def alert_records(question_id: str, impl: str, cur: Dict[str, Any], prev: Dict[str, Any],
                  esc: Optional[Dict[str, Any]] = None, drift_value: Optional[float] = None) -> List[Dict[str, Any]]:
    """The FM-2.5 revert triggers for one implementation, plus ECE and drift - only with enough rows - as records
    {question, impl, kind: precision_drop | ece | escalation | drift, text} (FM-11 auto-revert reads the kind)."""
    out: List[Dict[str, Any]] = []

    def add(kind: str, text: str, who: str = impl) -> None:
        out.append({"question": question_id, "impl": who, "kind": kind, "text": text})

    cg, pg = (cur or {}).get("vs_gold") or {}, (prev or {}).get("vs_gold") or {}
    if (cg.get("n") or 0) >= MIN_N and (pg.get("n") or 0) >= MIN_N:
        a, b = cg.get("precision_at_tau"), pg.get("precision_at_tau")
        if a is not None and b is not None and b - a >= PRECISION_DROP:
            add("precision_drop", f"{question_id} {impl}: precision at tau fell {b:.3f} -> {a:.3f}")
    if (cg.get("n") or 0) >= MIN_N and (cg.get("ece") or 0) > ECE_MAX:
        add("ece", f"{question_id} {impl}: ECE {cg['ece']:.3f} > {ECE_MAX}")
    if esc and esc.get("rate") is not None and (esc.get("took", 0) + esc.get("handed", 0)) >= MIN_N \
            and esc["rate"] > ESCALATION_MAX:
        add("escalation", f"{question_id}: escalation {esc['rate']:.0%} > {ESCALATION_MAX:.0%}", "")
    if drift_value is not None and drift_value > DRIFT_MAX:
        add("drift", f"{question_id}: live answers drifted (TVD {drift_value:.2f} > {DRIFT_MAX})", "")
    return out


def alerts(question_id: str, impl: str, cur: Dict[str, Any], prev: Dict[str, Any],
           esc: Optional[Dict[str, Any]] = None, drift_value: Optional[float] = None) -> List[str]:
    """The FM-2.5 revert triggers for one implementation, plus ECE and drift - only with enough rows."""
    return [r["text"] for r in alert_records(question_id, impl, cur, prev, esc, drift_value)]


def report(shadows_cur: List[Dict[str, Any]], shadows_prev: List[Dict[str, Any]], live_cur: List[Dict[str, Any]],
           live_prev: List[Dict[str, Any]], tau: float = 0.95) -> Dict[str, Any]:
    """The whole calibration: shadows rows {question_id, impl, answer, p, would_take, live (answer or None when
    the live decision is missing), gold}; live rows {question_id, decision_id, run_id, decider, answer, stored
    (bool | None when the surface keeps no inputs)}."""
    out: Dict[str, Any] = {"questions": {}, "alerts": [], "alert_items": []}
    cosigned = {r.get("for") for r in shadows_cur if r.get("would_take")}
    esc = escalation(live_cur, cosigned)
    qs = sorted({r["question_id"] for r in shadows_cur + shadows_prev + live_cur})
    for q in qs:
        lc = [d for d in live_cur if d["question_id"] == q]
        lp = [d for d in live_prev if d["question_id"] == q]
        sc = [r for r in shadows_cur if r["question_id"] == q]
        sp = [r for r in shadows_prev if r["question_id"] == q]
        stored = [d for d in lc if d.get("stored") is not None]
        dv = drift([d.get("answer") for d in lc], [d.get("answer") for d in lp])
        qo: Dict[str, Any] = {
            "live": len(lc), "deciders": dict(Counter(d.get("decider") for d in lc)),
            "drop_rate": round(sum(1 for d in stored if not d["stored"]) / len(stored), 3) if stored else None,
            "orphan_rate": round(sum(1 for r in sc if r.get("live") is None) / len(sc), 3) if sc else None,
            "drift": dv, "escalation": esc.get(q), "implementations": {}}
        for impl in sorted({r["impl"] for r in sc + sp}):
            cur = implementation_stats(q, [r for r in sc if r["impl"] == impl and r.get("live") is not None], tau)
            prev = implementation_stats(q, [r for r in sp if r["impl"] == impl and r.get("live") is not None], tau)
            qo["implementations"][impl] = {"current": cur, "previous": prev}
            items = alert_records(q, impl, cur, prev)
            out["alerts"] += [r["text"] for r in items]
            out["alert_items"] += items
        items = alert_records(q, "", {}, {}, esc.get(q), dv)
        out["alerts"] += [r["text"] for r in items]
        out["alert_items"] += items
        out["questions"][q] = qo
    return out
