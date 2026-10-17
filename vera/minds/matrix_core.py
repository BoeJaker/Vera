"""The candidate matrix (four-minds FM-2; owner, 17 Oct: "there are many applications and models that could be
introduced - introduce and test them in parallel").

Every application (a decision point with stored inputs: the loop's six, intel's signal questions, discovery's
on-topic verdict, ...) x every candidate that can answer it, scored in shadow on the same live decisions, on ONE
combined nightly schedule that grows as candidates and applications are added:

- small LLMs - every completion model on the nodes up to MAX_PARAMS_B, read from model.callable (not a hand list),
  one token by logprobs (decide.shadow.score with that model);
- embedding judges - kNN over past decisions, label cards (MiniLM);
- NLP-node models - zero-shot (DeBERTa-base MNLI), NLI (DeBERTa-large), the cross-encoder (ms-marco MiniLM), each
  against the application's label cards;
- trained heads, as MW-6 binds them.

NLP and embedding judges are cheap: every application, every night. LLM pairs cost a model call per decision, so they
rotate: each night the pairs scored longest ago (never-scored first) up to a budget - so every pair is scored again
within a few nights and none waits for another's result.

decide.shadow.report is the raw matrix; report() turns it into one table per application with the untested pairs
named.

Pure: no I/O.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_PARAMS_B = 10.0
#: F16 weights of a large model are too heavy to swap in at night beside the warm models
MAX_F16_PARAMS_B = 3.0
LLM_JOBS_PER_NIGHT = 48
NLP_IMPLS = ("zeroshot:deberta-base", "nli:deberta-large", "rerank:msmarco-minilm")

_P = re.compile(r"^\s*([\d.]+)\s*([KMB])", re.I)


def params_b(text: Any) -> Optional[float]:
    m = _P.match(str(text or ""))
    if not m:
        return None
    n = float(m.group(1))
    return {"k": n / 1e6, "m": n / 1e3, "b": n}[m.group(2).lower()]


def llm_candidates(rows: Iterable[Mapping[str, Any]], max_b: float = MAX_PARAMS_B,
                   exclude: Iterable[str] = ()) -> List[str]:
    """Every callable completion LLM up to max_b, one per (family, size, quant) - from model.callable's rows."""
    rows = list(rows)
    seen: Dict[Tuple[str, str, str], str] = {}
    ex = set(exclude)
    for r in rows:
        if r.get("family") != "ollama" or not str(r.get("id") or "").startswith("llm.generate:"):
            continue
        name = str(r.get("model") or str(r["id"]).split(":", 1)[1])
        f = r.get("facts") or {}
        ab = set(f.get("abilities") or [])
        b = params_b(f.get("params"))
        if name in ex or "completion" not in ab or b is None or b > max_b:
            continue
        if str(f.get("quant") or "").upper() == "F16" and b > MAX_F16_PARAMS_B:
            continue
        key = (str(f.get("family")), str(f.get("params")), str(f.get("quant")))
        if key not in seen or len(name) < len(seen[key]):
            seen[key] = name
    return sorted(seen.values(), key=lambda n: (params_b(_facts_params(rows, n)) or 0, n))


def _facts_params(rows: Iterable[Mapping[str, Any]], name: str) -> str:
    for r in rows:
        if str(r.get("model") or "") == name:
            return str((r.get("facts") or {}).get("params") or "")
    return ""


def rotate(pairs: Sequence[str], last: Mapping[str, str], budget: int = LLM_JOBS_PER_NIGHT) -> List[str]:
    """Tonight's LLM pairs: never-scored first, then the longest ago, up to the budget (ties keep their order)."""
    order = {p: i for i, p in enumerate(pairs)}
    ranked = sorted(pairs, key=lambda p: (last.get(p, "") != "", last.get(p, ""), order[p]))
    return ranked[:max(0, int(budget))]


def softmax(scores: Mapping[str, float], temp: float = 1.0) -> Dict[str, float]:
    if not scores:
        return {}
    top = max(scores.values())
    ex = {k: math.exp((v - top) / max(1e-6, temp)) for k, v in scores.items()}
    tot = sum(ex.values())
    return {k: v / tot for k, v in ex.items()}


def vote(question: Mapping[str, Any], label_probs: Mapping[str, float]) -> Tuple[Optional[str], Optional[float]]:
    """(label, p) from probabilities over the question's labels, in judge_core's convention: binary p is the
    probability of the positive (first) label, choice p is the chosen label's."""
    labels = list((question.get("labels") or {}).values())
    probs = {k: float(v) for k, v in label_probs.items() if k in labels}
    if not probs:
        return None, None
    tot = sum(probs.values()) or 1.0
    probs = {k: v / tot for k, v in probs.items()}
    best = max(probs, key=probs.get)
    if question.get("kind") == "binary":
        return best, round(probs.get(labels[0], 0.0), 4)
    return best, round(probs[best], 4)


def report(shadow: Mapping[str, Any], candidates: Sequence[str], pending: Mapping[str, str]) -> Dict[str, Any]:
    """One table per application: each candidate's n, agreement with the live decision, accuracy on outcomes (when
    there are outcomes), latency - and the candidates never scored there."""
    live = shadow.get("live") or {}
    apps: Dict[str, Any] = {}
    for q, impls in (shadow.get("questions") or {}).items():
        rows = []
        for impl, v in (impls or {}).items():
            vo = v.get("vs_outcomes") or {}
            rows.append({"candidate": impl, "n": v.get("n"), "agreement": v.get("agreement"),
                         "outcomes_n": vo.get("n") or 0, "accuracy": vo.get("accuracy"),
                         "ms_p50": v.get("ms_p50")})
        rows.sort(key=lambda r: (-(r["accuracy"] if r["accuracy"] is not None and r["outcomes_n"] >= 30 else -1),
                                 -(r["agreement"] or 0)))
        scored = {r["candidate"] for r in rows}
        apps[q] = {"live": live.get(q) or {}, "candidates": rows,
                   "untested": [c for c in candidates if c not in scored]}
    return {"applications": apps, "pending": dict(pending),
            "pairs_scored": sum(len(a["candidates"]) for a in apps.values()),
            "pairs_untested": sum(len(a["untested"]) for a in apps.values())}


def head_vote(question: Mapping[str, Any], pred: Any) -> Tuple[Optional[str], Optional[float]]:
    """A trained head's output as (label, p): one number = p(positive, the first label) for a binary question; one
    number per label = a distribution (choice or binary)."""
    labels = list((question.get("labels") or {}).values())
    vals = pred if isinstance(pred, (list, tuple)) else [pred]
    try:
        vals = [float(v) for v in vals]
    except (TypeError, ValueError):
        return None, None
    if not vals or not labels:
        return None, None
    if len(vals) == 1:
        if question.get("kind") != "binary" or len(labels) < 2:
            return None, None
        p = min(1.0, max(0.0, vals[0]))
        return (labels[0] if p >= 0.5 else labels[1]), round(p, 4)
    if len(vals) != len(labels):
        return None, None
    return vote(question, dict(zip(labels, vals)))
