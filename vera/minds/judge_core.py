"""A small model as a System 1 shadow judge (four-minds FM-2, the 2026-10-09 model survey).

A stored decision's inputs (the training store) are put to a small LLM as a single-token question,
and the probability of each label comes from the logprobs of that one token (softmax over the label
tokens only, thinking off). The answer is recorded as a SHADOW decision for the same question,
linked to the live decision (`detail.for`), so agreement and calibration are read from the ledger.

First question: `loop.step_verify`, where offline the 2B model ranked the big model's verdicts at
AUC 0.83 (n=39) and the deployed NLI model at 0.60.

Pure: no I/O.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

#: per question: the system prompt, how to build the user message from stored inputs, the label each
#: answer token means
QUESTIONS: Dict[str, Dict[str, Any]] = {
    "loop.step_verify": {
        "system": ("You check whether one step of an agent's work met its success criterion. You get the "
                   "criterion and the step's result. Answer with one word: yes if the result shows the "
                   "criterion was met, no if it does not."),
        "fields": ("success", "result"),
        "labels": {"yes": "met", "no": "not_met"},
    },
}
MAX_INPUT_CHARS = 3000


def user_message(question_id: str, state: Dict[str, Any]) -> Optional[str]:
    """The question put to the model, or None when the stored inputs lack what it needs."""
    q = QUESTIONS.get(question_id)
    if not q or not isinstance(state, dict):
        return None
    if question_id == "loop.step_verify":
        crit, res = str(state.get("success") or "").strip(), str(state.get("result") or "").strip()
        if not crit or not res:
            return None
        return f"Success criterion: {crit}\n\nStep result: {res[:MAX_INPUT_CHARS]}\n\nWas the criterion met?"
    return None


def label_probs(first_token_tops: Iterable[Dict[str, Any]], words: Iterable[str]) -> Tuple[Dict[str, float], float]:
    """{word: probability} over the label words, renormalised over the label mass, and the mass itself
    (how much of the top-N probability fell on any label; low mass = the model wanted to say something else)."""
    words = [w.lower() for w in words]
    raw = {w: 0.0 for w in words}
    for t in first_token_tops or []:
        try:
            tok, lp = str(t.get("token") or "").strip().lower(), float(t.get("logprob"))
        except (TypeError, ValueError):
            continue
        if tok in raw:
            raw[tok] += math.exp(lp)
    mass = sum(raw.values())
    if mass <= 0:
        return {}, 0.0
    return {w: v / mass for w, v in raw.items()}, mass


def verdict(question_id: str, probs: Dict[str, float]) -> Tuple[Optional[str], Optional[float]]:
    """(answer label, probability of the positive label) from label-word probabilities."""
    q = QUESTIONS.get(question_id) or {}
    labels = q.get("labels") or {}
    if not probs or not labels:
        return None, None
    word = max(probs, key=probs.get)
    pos = next(iter(labels))                       # the first word is the positive label ("yes")
    return labels.get(word), round(probs.get(pos, 0.0), 4)


def shadow_decision(live: Dict[str, Any], answer: str, p: Optional[float], implementation: str,
                    mass: float = 0.0, ms: int = 0) -> Dict[str, Any]:
    """The shadow row for a live decision: same question, labels, state reference and run; the small
    model as the implementation; linked by detail.for."""
    return LC.make_decision(
        live["question_id"], answer, origin=live.get("origin") or "user", surface=live.get("surface") or "loop",
        qtype=live.get("qtype") or "binary", labels=list(live.get("labels") or []),
        state={"for": live["decision_id"]}, state_ref=dict(live.get("state_ref") or {}),
        decider="student", implementation=str(implementation)[:80], mode="shadow", p=p, ms=int(ms),
        minds_digest=live.get("minds_digest") or "default", run_id=live.get("run_id"),
        session_id=live.get("session_id"),
        detail={"for": live["decision_id"], "label_mass": round(float(mass), 4)})


def in_window(hour: int, start: int = 23, end: int = 6) -> bool:
    """start <= hour < end on a clock that wraps at midnight."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def agreement(pairs: List[Tuple[str, str, Optional[float]]], positive: str) -> Dict[str, Any]:
    """pairs: (live answer, shadow answer, shadow p(positive)). Agreement rate and the AUC of p
    against the live answer."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    agree = sum(1 for a, b, _ in pairs if a == b) / n
    pos = [p for a, _, p in pairs if a == positive and p is not None]
    neg = [p for a, _, p in pairs if a != positive and p is not None]
    auc = (sum((x > y) + 0.5 * (x == y) for x in pos for y in neg) / (len(pos) * len(neg))) if pos and neg else None
    return {"n": n, "agreement": round(agree, 3), "auc": None if auc is None else round(auc, 3),
            "live_positive": len(pos), "live_negative": len(neg)}
