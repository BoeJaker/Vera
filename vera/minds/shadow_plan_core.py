"""The night's shadow-judge jobs, read from the model bindings (four-minds B3; PROPOSAL-model-bindings-2026-10-10).

The judges declare what they are with `@capability(..., serves=binding(...))`: the decision questions they answer
and their System 1 tier. The night job used to list its judges by hand; it now asks the registry, so a new judge
or student - an ML Workshop head (MW-6) bound to a question - is judged the next night without editing the job.

- a tier-4 LLM judge (family ollama) is asked per model and per question it serves;
- any other judge (tier 1-3) runs once over every question, lowest tier first - the kNN judge stores the decision
  vectors the card judge then reads;
- only capabilities under the judge prefixes take part, because they share one calling convention
  (question_id, limit, since_hours).

The job keys are the ones the night job always used ("<question>|<model>", the cap's last name part), so job ids
and the once-a-night guard do not change. The ledger keeps its existing implementation ids (llm:<model>, knn,
card:minilm): rewriting them to binding ids would break the continuity of the shadow history.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

JUDGE_PREFIXES = ("decide.shadow.", "decide.student.")
LLM_FAMILY = "ollama"
ALL_LIMIT = 400
SINCE_HOURS = 48


def judge_bindings(registry: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Every binding of a judge capability that answers at least one question, with its cap name."""
    out = []
    for name, entry in (registry or {}).items():
        if not str(name).startswith(JUDGE_PREFIXES):
            continue
        for b in (entry or {}).get("models") or []:
            if isinstance(b, Mapping) and b.get("questions"):
                out.append(dict(b, cap=str(b.get("cap") or name)))
    return out


def plan(bindings: Iterable[Mapping[str, Any]], questions: Sequence[str], models: Sequence[str],
         night_cap: int) -> List[Tuple[str, str, Dict[str, Any], str]]:
    """[(cap, title, arguments, key)] in run order: the LLM judges, then the others by tier."""
    known = list(questions)
    llm: List[Tuple[str, str, Dict[str, Any], str]] = []
    rest: List[Tuple[int, str, Tuple[str, str, Dict[str, Any], str]]] = []
    for b in bindings:
        cap = str(b.get("cap") or "")
        served = [q for q in known if q in set(b.get("questions") or [])]
        if not cap or not served:
            continue
        if str(b.get("family")) == LLM_FAMILY:
            for model in models:
                for qid in served:
                    llm.append((cap, f"shadow judge: {qid} ({model})",
                                {"question_id": qid, "model": model, "limit": int(night_cap),
                                 "since_hours": SINCE_HOURS}, f"{qid}|{model}"))
        else:
            short = cap.rsplit(".", 1)[-1]
            rest.append((int(b.get("tier") or 0), cap,
                         (cap, f"shadow judge: {short} ({b.get('id') or cap}), all questions",
                          {"question_id": "all", "limit": ALL_LIMIT, "since_hours": SINCE_HOURS}, short)))
    # the LLM jobs model by model, as before; the others lowest tier first
    llm.sort(key=lambda j: (list(models).index(j[2]["model"]), known.index(j[2]["question_id"])))
    rest.sort(key=lambda r: (r[0], r[1]))
    return llm + [r[2] for r in rest]
