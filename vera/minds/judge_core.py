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

MAX_INPUT_CHARS = 3000
MAX_INPUT_CHARS_FIELD = 1500
#: the live verifier's whole evidence prompt (tool calls, last action, fetched text, filesystem check) - what a
#: student must see to judge the same thing the big model judged
MAX_EVIDENCE_CHARS = 14000
#: the live verifier ends its rules asking for JSON; a student answers the same rules in one word instead
STUDENT_VERIFY_ANSWER = ("Answer with ONE word: yes if the evidence shows the success criterion was met, "
                         "no if it does not.")
STUDENT_CONTROLLER_ANSWER = ("Answer with ONE letter: A = continue with the queue, B = replan the remaining "
                             "steps, C = insert a step before continuing, D = stop (the goal is met or cannot "
                             "progress).")
#: how a student ends the live model's own instructions, per question it can stand in for
STUDENT_ANSWERS = {"loop.step_verify": STUDENT_VERIFY_ANSWER, "loop.controller_action": STUDENT_CONTROLLER_ANSWER}

#: per question: the system prompt, the label each answer token means (binary questions: the first
#: token is the positive label; choice questions answer with one letter)
QUESTIONS: Dict[str, Dict[str, Any]] = {
    "loop.step_verify": {
        "system": ("You check whether one step of an agent's work met its success criterion. You get the "
                   "criterion and the step's result. Answer with one word: yes if the result shows the "
                   "criterion was met, no if it does not."),
        "labels": {"yes": "met", "no": "not_met"}, "kind": "binary",
    },
    "loop.step_done": {
        "system": ("An agent is working on one step. You get the step's goal and what its tool calls have produced "
                   "so far. Answer with one word: yes if what it has is enough to finish the step now, no if it "
                   "must keep working."),
        "labels": {"yes": "end", "no": "continue"}, "kind": "binary",
    },
    "loop.prestep_needed": {
        "system": ("Before an agent runs one step of a larger goal, decide whether it must first gather facts it "
                   "does not have (data, parameters, decisions it cannot derive). Answer with one word: yes if it "
                   "must gather something first, no if what is already known is enough to start."),
        "labels": {"yes": "needed", "no": "not_needed"}, "kind": "binary",
    },
    "loop.intent": {
        "system": ("Classify the KIND of work a goal asks an agent to do. Answer with one letter: A = build "
                   "(create or write something: code, an app, a document), B = research (look up external or "
                   "current information), C = action (operate, configure or fix a live system), D = mixed "
                   "(look things up, then build with them)."),
        "labels": {"a": "build", "b": "research", "c": "action", "d": "mixed"}, "kind": "choice",
    },
    "loop.tier": {
        "system": ("Estimate how much work a goal is for an agent. Answer with one letter: A = single (one tool "
                   "call answers it), B = simple (1-3 steps), C = complex (several steps, maybe a plan), "
                   "D = strategic (a multi-day project)."),
        "labels": {"a": "single", "b": "simple", "c": "complex", "d": "strategic"}, "kind": "choice",
    },
    "loop.controller_action": {
        "system": ("You steer an agent after each step of a goal. You get the goal, what is done, the last "
                   "result and the steps still queued. Answer with one letter: A = continue with the queue, "
                   "B = replan the remaining steps, C = insert a step before continuing, D = stop (the goal "
                   "is met or cannot progress)."),
        "labels": {"a": "continue", "b": "replan", "c": "insert", "d": "stop"}, "kind": "choice",
    },
}


def _s(v: Any, n: int = MAX_INPUT_CHARS_FIELD) -> str:
    return str(v or "").strip()[:n]


def _done_lines(done: Any) -> str:
    out = []
    for d in (done or [])[:20]:
        if isinstance(d, dict):
            ok = "met" if d.get("met") else ("done" if d.get("ok") else "failed")
            out.append(f"- {_s(d.get('title'), 120)} ({ok})")
    return "\n".join(out) or "(nothing yet)"


def user_message(question_id: str, state: Dict[str, Any]) -> Optional[str]:
    """The question put to the model, or None when the stored inputs lack what it needs."""
    q = QUESTIONS.get(question_id)
    if not q or not isinstance(state, dict):
        return None
    if question_id == "loop.step_verify":
        ev = str(state.get("evidence") or "").strip()
        if ev:                                    # the live verifier's own prompt: the same input
            return ev[:MAX_EVIDENCE_CHARS]
        crit, res = str(state.get("success") or "").strip(), str(state.get("result") or "").strip()
        if not crit or not res:
            return None
        return f"Success criterion: {crit}\n\nStep result: {res[:MAX_INPUT_CHARS]}\n\nWas the criterion met?"
    if question_id == "loop.step_done":
        res = _s(state.get("result"), MAX_INPUT_CHARS)
        if not res:
            return None
        return (f"Step: {_s(state.get('step'), 200)}\nStep goal: {_s(state.get('step_goal'))}\n\n"
                f"Produced so far: {res}\n\nIs this enough to finish the step now?")
    if question_id == "loop.prestep_needed":
        if not _s(state.get("step_goal")) and not _s(state.get("step")):
            return None
        return (f"Overall goal: {_s(state.get('goal'), 600)}\nStep: {_s(state.get('step'), 200)}\n"
                f"Step goal: {_s(state.get('step_goal'))}\n\nAlready known:\n{_s(state.get('known')) or '(nothing yet)'}"
                "\n\nMust the step gather something first?")
    if question_id in ("loop.intent", "loop.tier"):
        goal = _s(state.get("goal"), MAX_INPUT_CHARS)
        return f"Goal: {goal}\n\nAnswer with one letter." if goal else None
    if question_id == "loop.controller_action":
        ev = str(state.get("evidence") or "").strip()
        if ev:                                    # the live controller's own prompt: the same input
            return ev[:MAX_EVIDENCE_CHARS]
        goal = _s(state.get("goal"), 800)
        if not goal:
            return None
        queue = [_s(q, 120) for q in (state.get("queue") or [])[:12]]
        return (f"Goal: {goal}\nDone when: {_s(state.get('done_when'), 300) or '(not stated)'}\n\n"
                f"Steps so far:\n{_done_lines(state.get('done'))}\n\nLast step: {_s(state.get('after_step'), 200)}\n"
                f"Its result: {_s(state.get('result'))}\n\nStill queued: {', '.join(queue) or '(nothing)'}"
                f"\nSteps left in the budget: {state.get('steps_left')}\n\nAnswer with one letter.")
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


def student_system(live_system: str, question_id: str = "loop.step_verify") -> str:
    """The live model's own instructions, asking for one word or letter instead of JSON."""
    s = str(live_system or "").strip()
    cut = s.rfind("Respond ONLY with JSON")
    return ((s[:cut] if cut >= 0 else s).rstrip() + "\n" + STUDENT_ANSWERS[question_id]).strip()


def system_for(question_id: str, state: Optional[Dict[str, Any]] = None) -> str:
    """The system prompt for a question: the live rules when the stored inputs carry them, else the generic one."""
    if question_id in STUDENT_ANSWERS and isinstance(state, dict) and state.get("evidence_system"):
        return student_system(state["evidence_system"], question_id)
    return (QUESTIONS.get(question_id) or {}).get("system", "")


def confidence(question_id: str, answer: Optional[str], p: Optional[float]) -> Optional[float]:
    """How sure the student is of the answer it gave (binary p is p(positive); choice p is already its own)."""
    if answer is None or p is None:
        return None
    pos = positive_label(question_id)
    if pos is None or answer == pos:
        return float(p)
    return 1.0 - float(p)


def takes(question_id: str, answer: Optional[str], p: Optional[float], tau: float, allowed: Iterable[str]) -> bool:
    """student_first: may the student's answer stand without the big model? Only an allowed label, only at
    confidence >= tau."""
    c = confidence(question_id, answer, p)
    return c is not None and answer in set(allowed) and c >= float(tau)


def verdict(question_id: str, probs: Dict[str, float]) -> Tuple[Optional[str], Optional[float]]:
    """(answer label, p). Binary: p is the probability of the positive label (the first). Choice: p is the
    probability of the chosen label."""
    q = QUESTIONS.get(question_id) or {}
    labels = q.get("labels") or {}
    if not probs or not labels:
        return None, None
    word = max(probs, key=probs.get)
    if q.get("kind") == "choice":
        return labels.get(word), round(probs.get(word, 0.0), 4)
    pos = next(iter(labels))                       # the first word is the positive label ("yes")
    return labels.get(word), round(probs.get(pos, 0.0), 4)


def positive_label(question_id: str) -> Optional[str]:
    """The positive label of a binary question (for AUC); None for choice questions."""
    q = QUESTIONS.get(question_id) or {}
    if q.get("kind") != "binary":
        return None
    return next(iter((q.get("labels") or {}).values()), None)


def shadow_decision(live: Dict[str, Any], answer: str, p: Optional[float], implementation: str,
                    mass: float = 0.0, ms: int = 0, would_take: Optional[bool] = None) -> Dict[str, Any]:
    """The shadow row for a live decision: same question, labels, state reference and run; the small
    model as the implementation; linked by detail.for. would_take (a co-signed answer that would have stood
    under first) is kept in the detail - the precision of the student's takes is read from those rows."""
    detail = {"for": live["decision_id"], "label_mass": round(float(mass), 4)}
    if would_take is not None:
        detail["would_take"] = bool(would_take)
    return LC.make_decision(
        live["question_id"], answer, origin=live.get("origin") or "user", surface=live.get("surface") or "loop",
        qtype=live.get("qtype") or "binary", labels=list(live.get("labels") or []),
        state={"for": live["decision_id"]}, state_ref=dict(live.get("state_ref") or {}),
        decider="student", implementation=str(implementation)[:80], mode="shadow", p=p, ms=int(ms),
        minds_digest=live.get("minds_digest") or "default", run_id=live.get("run_id"),
        session_id=live.get("session_id"),
        detail=detail)


def in_window(hour: int, start: int = 23, end: int = 6) -> bool:
    """start <= hour < end on a clock that wraps at midnight."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def agreement(pairs: List[Tuple[str, str, Optional[float]]], positive: Optional[str]) -> Dict[str, Any]:
    """pairs: (live answer, shadow answer, shadow p(positive)). Agreement rate and the AUC of p
    against the live answer."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    agree = sum(1 for a, b, _ in pairs if a == b) / n
    if positive is None:                           # a choice question: agreement only
        return {"n": n, "agreement": round(agree, 3), "auc": None}
    pos = [p for a, _, p in pairs if a == positive and p is not None]
    neg = [p for a, _, p in pairs if a != positive and p is not None]
    auc = (sum((x > y) + 0.5 * (x == y) for x in pos for y in neg) / (len(pos) * len(neg))) if pos and neg else None
    return {"n": n, "agreement": round(agree, 3), "auc": None if auc is None else round(auc, 3),
            "live_positive": len(pos), "live_negative": len(neg)}


def true_label(question_id: str, live_answer: Any, outcome_label: Any) -> Optional[str]:
    """The right answer of a binary question, from the live decision's outcome: the live answer when the
    outcome says it was correct, the other label when it says wrong; None otherwise (no outcome, a choice
    question, or an outcome that is not a verdict)."""
    q = QUESTIONS.get(question_id) or {}
    if q.get("kind") != "binary":
        return None
    labels = list((q.get("labels") or {}).values())
    if live_answer not in labels or outcome_label not in ("correct", "wrong"):
        return None
    if outcome_label == "correct":
        return live_answer
    return next(l for l in labels if l != live_answer)


def calibration(rows: List[Tuple[Optional[float], str]], positive: str, bins: int = 10,
                thresholds: Iterable[float] = (0.5, 0.7, 0.8, 0.9)) -> Dict[str, Any]:
    """rows: (shadow p(positive), true label). Accuracy at 0.5, expected calibration error, and precision /
    coverage when the shadow answers only above each confidence threshold (the FM-2.5 promotion test)."""
    rows = [(float(p), y) for p, y in rows if p is not None and y is not None]
    n = len(rows)
    if not n:
        return {"n": 0}
    acc = sum(1 for p, y in rows if (p >= 0.5) == (y == positive)) / n
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inb = [(p, y) for p, y in rows if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if inb:
            conf = sum(p for p, _ in inb) / len(inb)
            freq = sum(1 for _, y in inb if y == positive) / len(inb)
            ece += len(inb) / n * abs(conf - freq)
    at = {}
    for t in thresholds:
        sure = [(p, y) for p, y in rows if max(p, 1 - p) >= t]
        right = sum(1 for p, y in sure if (p >= 0.5) == (y == positive))
        at[str(t)] = {"coverage": round(len(sure) / n, 3),
                      "precision": round(right / len(sure), 3) if sure else None}
    return {"n": n, "accuracy": round(acc, 3), "ece": round(ece, 3), "at_confidence": at}


def choice_calibration(rows: List[Tuple[Any, Optional[float], Any]], bins: int = 10,
                       thresholds: Iterable[float] = (0.5, 0.7, 0.8, 0.9)) -> Dict[str, Any]:
    """rows: (shadow answer, shadow p of that answer, gold). The same shape as `calibration` for a question with
    more than two labels: accuracy, expected calibration error (confidence p against being right), and precision /
    coverage when the shadow answers only at p >= each threshold."""
    rows = [(a, float(p), g) for a, p, g in rows if p is not None and g is not None]
    n = len(rows)
    if not n:
        return {"n": 0}
    acc = sum(1 for a, _, g in rows if a == g) / n
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inb = [(a, p, g) for a, p, g in rows if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if inb:
            conf = sum(p for _, p, _ in inb) / len(inb)
            right = sum(1 for a, _, g in inb if a == g) / len(inb)
            ece += len(inb) / n * abs(conf - right)
    at = {}
    for t in thresholds:
        sure = [(a, g) for a, p, g in rows if p >= t]
        at[str(t)] = {"coverage": round(len(sure) / n, 3),
                      "precision": round(sum(1 for a, g in sure if a == g) / len(sure), 3) if sure else None}
    return {"n": n, "accuracy": round(acc, 3), "ece": round(ece, 3), "at_confidence": at}


def live_accuracy(rows: Iterable[Tuple[Any, ...]]) -> Dict[str, Any]:
    """rows: (live answer, gold[, not_label]), one per live decision: the bar a student has to clear. A decision
    known only to be wrong (not_label) counts as a live miss - leaving those out would score the live model only
    where it was right."""
    right = n = 0
    for row in rows:
        a, g, nl = (tuple(row) + (None,))[:3]
        if g is not None:
            n += 1
            right += a == g
        elif nl is not None:
            n += 1
    if not n:
        return {"n": 0}
    return {"n": n, "accuracy": round(right / n, 3)}


def avoids_known_wrong(rows: Iterable[Tuple[Any, Any]]) -> Dict[str, Any]:
    """rows: (shadow answer, the live answer known to be wrong): how often the shadow chose something else."""
    rows = [(a, nl) for a, nl in rows if nl is not None]
    if not rows:
        return {"n": 0}
    return {"n": len(rows), "rate": round(sum(1 for a, nl in rows if a != nl) / len(rows), 3)}


KNN_IMPL = "knn:minilm"
KNN_K = 7
KNN_MIN_NEIGHBOURS = 5


def _unit(v: Iterable[float]) -> List[float]:
    v = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def knn_vote(question_id: str, qvec: Iterable[float], pool: Iterable[Dict[str, Any]], exclude_run: str = "",
             k: int = KNN_K, min_neighbours: int = KNN_MIN_NEIGHBOURS) -> Tuple[Optional[str], Optional[float], int]:
    """The live answers of the k most similar past decisions, weighted by similarity: (answer, p, neighbours).
    p is the vote share of the positive label for binary questions, of the chosen label for choice ones.
    Pool rows: {vec (unit), answer, run}. Rows of the same run are skipped (no copying itself); (None, None, n)
    when fewer than min_neighbours remain or nothing votes."""
    q = QUESTIONS.get(question_id) or {}
    labels = list((q.get("labels") or {}).values())
    qv = _unit(qvec or [])
    rows = [r for r in pool if r.get("run") != exclude_run and r.get("vec") and len(r["vec"]) == len(qv)
            and r.get("answer") in labels]
    if not qv or len(rows) < max(1, int(min_neighbours)):
        return None, None, len(rows)
    near = sorted(((sum(a * b for a, b in zip(qv, r["vec"])), r) for r in rows), key=lambda x: -x[0])[:max(1, int(k))]
    return vote_from_neighbours(question_id, [(s, r["answer"]) for s, r in near], len(rows), min_neighbours)


def vote_from_neighbours(question_id: str, neighbours: Iterable[Tuple[float, Any]], eligible: int,
                         min_neighbours: int = KNN_MIN_NEIGHBOURS) -> Tuple[Optional[str], Optional[float], int]:
    """knn_vote's vote over neighbours found elsewhere (an NLP node's POST /decide, FM-9.1): (answer, p,
    eligible). `eligible` is how many rows could have been neighbours (the run's own excluded)."""
    q = QUESTIONS.get(question_id) or {}
    labels = list((q.get("labels") or {}).values())
    if eligible < max(1, int(min_neighbours)):
        return None, None, eligible
    vote: Dict[str, float] = {}
    for s, ans in neighbours:
        if ans in labels:
            vote[ans] = vote.get(ans, 0.0) + max(0.0, float(s))
    total = sum(vote.values())
    if total <= 0:
        return None, None, eligible
    answer = max(vote, key=vote.get)
    if q.get("kind") == "binary":
        p = vote.get(labels[0], 0.0) / total
    else:
        p = vote[answer] / total
    return answer, round(p, 4), eligible


def latency(ms: Iterable[int]) -> Dict[str, Any]:
    """{ms_p50, ms_p90} of a shadow judge's answers (each recorded its own call time), or {} when none did."""
    v = sorted(int(x) for x in ms if x)
    if not v:
        return {}
    return {"ms_p50": v[len(v) // 2], "ms_p90": v[min(len(v) - 1, int(len(v) * 0.9))]}


# ── the card-similarity judge (FM-2.1 stage-0: no training, no generation) ─────────────────────────────────────
# One short description per label, embedded once in the decision space (MiniLM-384 on a node); a decision takes the
# label whose card its own input vector is nearest. Strongest where the label is about WHAT the text is (intent,
# tier), weakest where it is about how two parts of it relate (a criterion vs a result) - measured, not assumed.
CARD_IMPL = "card:minilm"
CARD_TEMP = 20.0
CARDS: Dict[str, Dict[str, str]] = {
    "loop.step_verify": {
        "met": "The step's result shows its success criterion was fully met.",
        "not_met": "The step's result does not show its success criterion was met: something is missing, wrong "
                   "or failed."},
    "loop.step_done": {
        "end": "What the tool calls have produced is enough to finish this step now.",
        "continue": "The step needs more work: what has been produced so far is not enough."},
    "loop.prestep_needed": {
        "needed": "Facts, data or parameters must be gathered before this step can start.",
        "not_needed": "Everything needed to start this step is already known."},
    "loop.intent": {
        "build": "Create or write something: code, an app, a script, a file or a document.",
        "research": "Look up external or current information and report what was found.",
        "action": "Operate, configure or fix a live system or service.",
        "mixed": "Look things up first, then build something with what was found."},
    "loop.tier": {
        "single": "A single lookup or one tool call answers it.",
        "simple": "A short task of one to three steps.",
        "complex": "Several steps that need a plan.",
        "strategic": "A large multi-day project with many parts."},
    "loop.controller_action": {
        "continue": "Carry on with the queued steps as planned.",
        "replan": "The plan no longer fits what happened; replan the remaining steps.",
        "insert": "A step is missing; insert it before continuing.",
        "stop": "Stop now: the goal is met or cannot make progress."},
}


def card_vote(vec: Optional[Iterable[float]], card_vecs: Dict[str, Iterable[float]],
              temp: float = CARD_TEMP) -> Tuple[Optional[str], Optional[float]]:
    """(label, p) for the nearest card - p a softmax over the cosine similarities (temperature temp); (None, None)
    without a vector or cards."""
    if not vec or not card_vecs:
        return None, None
    v = _unit(vec)
    sims = {lab: sum(a * b for a, b in zip(v, _unit(cv))) for lab, cv in card_vecs.items() if cv}
    if not sims:
        return None, None
    top = max(sims.values())
    ex = {lab: math.exp(temp * (s - top)) for lab, s in sims.items()}
    best = max(sims, key=sims.get)
    return best, round(ex[best] / sum(ex.values()), 4)


# ── the candidate matrix (owner, 17 Oct): decision points beyond the loop, scored by every judge ──────────────────
# applications_core holds them in this file's shape; merged here so the LLM judges (any model), kNN, cards, zero-shot,
# NLI and the cross-encoder all score them with no further wiring. LOOP_QUESTIONS keeps the loop's own six apart
# (the loop student, the cascade switches and the live-model filters are about those).
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import applications_core as _APPS
except Exception:                        # worktree / app-free import
    from vera.minds import applications_core as _APPS  # type: ignore

LOOP_QUESTIONS = tuple(QUESTIONS)
for _q, _d in _APPS.QUESTIONS.items():
    QUESTIONS.setdefault(_q, _d)
for _q, _d in _APPS.CARDS.items():
    CARDS.setdefault(_q, _d)
_loop_user_message = user_message


def user_message(question_id: str, state: Dict[str, Any]) -> Optional[str]:  # noqa: F811 - extends the loop's
    """The question put to the model: an application's own builder, else the loop's."""
    if question_id in _APPS.QUESTIONS:
        return _APPS.user_message(question_id, state)
    return _loop_user_message(question_id, state)
