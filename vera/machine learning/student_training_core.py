"""System 1 student heads, trained by the ML Workshop (ml-workshop-compute MW-6;
four-minds FM-2.4, authorised by the owner 2026-10-11).

A student is a small classifier over the MiniLM-384 vector of a decision's
state text that answers one decision question (loop.step_done: will this step
end the work?). It is trained from `decide.export` gold on the node pool, at
night, and is used in SHADOW only - four-minds decides any promotion.

Pure: no Vera imports, so tests run anywhere. The capability module
(student_training.py) does the I/O.
Plan: shared-planning/ml-workshop-compute/PLAN.md (MW-6 design).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Trained first each night; every other two-label decision question
#: (judge_core.QUESTIONS, the candidate matrix's applications included) follows,
#: each trained once its gold passes trainable() (owner 18 Oct: more questions
#: are coming online).
QUESTIONS: Tuple[str, ...] = ("loop.step_done",)

#: Local hours a training run may start in (MONITOR-STUDENT-TRAINING.md:
#: night only, not during the owner's 18:00-01:00 load).
WINDOW = (1, 6)
MIN_GOLD = 50            # below this a question is not trained
MIN_CLASS = 10           # nor when either class has fewer examples (>= 2 per class in each CV fold)
FOLDS = 5                # stratified cross-validation folds (the evaluation)
AUC_MIN = 0.7            # a student below this is not bound
KEEP = 3                 # artifacts kept per question
ATTEMPTS = 3             # failed runs (errors) per question per night before giving up
DISK_MAX_PCT = 90.0      # nothing written above this
DIM = 384                # MiniLM-L6 (nlp.embed)
EPOCHS = 60
LR = 0.05
METRICS = ("accuracy", "auc", "ece", "precision_at_0_8", "coverage_at_0_8")


def in_window(hour: int, window: Tuple[int, int] = WINDOW) -> bool:
    a, b = window
    return a <= int(hour) < b


def guard(*, hour: int, disk_pct: Optional[float] = None, force: bool = False) -> Tuple[bool, str]:
    """May a training run start now? (ok, why). `force` skips the window only -
    never the disk rule. There is no busy-box check: the box is never quiet
    (owner, 18 Oct - background work is scheduled, not left waiting for an idle
    box), and a student needs no quiet anyway - it trains on a CPU node, never
    a GPU host, and the node scheduler admits it only where there is room."""
    if disk_pct is not None and disk_pct > DISK_MAX_PCT:
        return False, f"data disk at {disk_pct:.0f}% (> {DISK_MAX_PCT:.0f}%): nothing is written"
    if force:
        return True, "forced (window check skipped)"
    if not in_window(hour):
        return False, f"outside the training window {WINDOW[0]:02d}:00-{WINDOW[1]:02d}:00"
    return True, "ok"


def order_questions(binary: Iterable[str]) -> List[str]:
    """QUESTIONS first, then every other two-label question, sorted."""
    have = {str(q) for q in binary}
    return [q for q in QUESTIONS if q in have] + sorted(have - set(QUESTIONS))


def label_of(row: Mapping[str, Any]) -> Optional[str]:
    g = row.get("gold")
    return str(g) if g not in (None, "") else None


def trainable(rows: Sequence[Mapping[str, Any]], positive: str) -> Tuple[bool, str, Dict[str, int]]:
    """(ok, why, counts) for a question's exported rows."""
    gold = [r for r in rows if label_of(r) is not None]
    pos = sum(1 for r in gold if label_of(r) == positive)
    neg = len(gold) - pos
    counts = {"gold": len(gold), "positive": pos, "negative": neg,
              "train": sum(1 for r in gold if r.get("split") != "test"),
              "test": sum(1 for r in gold if r.get("split") == "test")}
    if len(gold) < MIN_GOLD:
        return False, f"{len(gold)} gold examples (< {MIN_GOLD})", counts
    if min(pos, neg) < MIN_CLASS:
        return False, f"a class has {min(pos, neg)} examples (< {MIN_CLASS})", counts
    if counts["test"] < 5:
        return False, f"{counts['test']} test examples (< 5)", counts
    return True, "ok", counts


def build_xy(rows: Sequence[Mapping[str, Any]], vectors: Mapping[str, Sequence[float]],
             positive: str) -> Dict[str, Any]:
    """Feature rows (the state vector) and 0/1 labels per split; rows without a
    vector or gold are left out (and counted)."""
    out: Dict[str, Any] = {"train": ([], [], []), "test": ([], [], []), "skipped": 0}
    for r in rows:
        lab = label_of(r)
        vec = vectors.get(str(r.get("id")))
        if lab is None or not vec or len(vec) != DIM:
            out["skipped"] += 1
            continue
        part = out["test" if r.get("split") == "test" else "train"]
        part[0].append([float(x) for x in vec])
        part[1].append([1.0 if lab == positive else 0.0])
        part[2].append(str(r.get("id")))
    return out


def split_val(X: List[List[float]], y: List[List[float]], frac: float = 0.15):
    """The last `frac` of train as validation (the export order is stable)."""
    n = len(X)
    k = max(1, int(round(n * frac))) if n > 4 else 0
    if not k:
        return X, y, X[-1:], y[-1:]
    return X[:-k], y[:-k], X[-k:], y[-k:]


def head_module(question_id: str) -> Dict[str, Any]:
    """The student: one dense layer over the state vector, sigmoid out."""
    mid = module_id(question_id)
    return {"id": mid, "name": f"student {question_id}",
            "description": f"System 1 student for {question_id}: logistic head over MiniLM-384 state vectors (shadow only)",
            "nodes": [{"id": "head", "type": "dense",
                       "params": {"in_features": DIM, "out_features": 1, "activation": "sigmoid"}}],
            "edges": []}


def module_id(question_id: str) -> str:
    return "student-" + "".join(c if c.isalnum() else "-" for c in question_id)


def metrics(p: Sequence[float], y: Sequence[float]) -> Dict[str, float]:
    """accuracy at 0.5, ROC AUC, expected calibration error (10 bins), and
    precision / coverage when acting only above 0.8 (either way)."""
    n = len(p)
    if not n:
        return {k: 0.0 for k in METRICS}
    pp = [min(1.0, max(0.0, float(v))) for v in p]
    yy = [1.0 if float(v) >= 0.5 else 0.0 for v in y]
    acc = sum(1 for a, b in zip(pp, yy) if (a >= 0.5) == (b == 1.0)) / n
    pos = [a for a, b in zip(pp, yy) if b == 1.0]
    neg = [a for a, b in zip(pp, yy) if b == 0.0]
    if pos and neg:
        wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
        auc = wins / (len(pos) * len(neg))
    else:
        auc = 0.5
    ece = 0.0
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        idx = [j for j, a in enumerate(pp) if (lo <= a < hi) or (i == 9 and a == 1.0)]
        if idx:
            conf = sum(pp[j] for j in idx) / len(idx)
            frac = sum(yy[j] for j in idx) / len(idx)
            ece += len(idx) / n * abs(conf - frac)
    sure = [(a, b) for a, b in zip(pp, yy) if a >= 0.8 or a <= 0.2]
    prec = (sum(1 for a, b in sure if (a >= 0.5) == (b == 1.0)) / len(sure)) if sure else 0.0
    return {"accuracy": round(acc, 4), "auc": round(auc, 4), "ece": round(ece, 4),
            "precision_at_0_8": round(prec, 4), "coverage_at_0_8": round(len(sure) / n, 4)}


def thresholds(base_rate: float = 0.5) -> Dict[str, Tuple[float, str]]:
    """What a report checks (metric -> (threshold, direction)). Accuracy must at
    least match always answering the majority label (`base_rate` = share of the
    positive label). A passing report gets the student bound (shadow only); it
    promotes nothing (four-minds' FM-2.5 rule does)."""
    majority = round(max(base_rate, 1.0 - base_rate), 4)
    return {"accuracy": (max(0.5, majority), "maximize"), "auc": (AUC_MIN, "maximize"),
            "ece": (0.2, "minimize"), "precision_at_0_8": (0.5, "maximize"),
            "coverage_at_0_8": (0.0, "maximize")}


def stratified_folds(y: Sequence[float], k: int = FOLDS) -> List[List[int]]:
    """k folds of row indexes, each class dealt round-robin in order (the export
    order is stable, so the folds are too)."""
    folds: List[List[int]] = [[] for _ in range(k)]
    for cls in (1.0, 0.0):
        idx = [i for i, v in enumerate(y) if (1.0 if float(v) >= 0.5 else 0.0) == cls]
        for j, i in enumerate(idx):
            folds[j % k].append(i)
    return [sorted(f) for f in folds]


def prune(paths: Iterable[Tuple[str, float]], keep: int = KEEP) -> List[str]:
    """Which artifacts to delete: all but the `keep` newest ((path, mtime))."""
    ordered = sorted(paths, key=lambda x: -x[1])
    return [p for p, _ in ordered[keep:]]
