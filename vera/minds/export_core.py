"""The training and evaluation set of the decision questions (four-minds FM-2.2, decide.export).

One example per live decision whose inputs are in the training store:
{id, question: {id, type, instructions, options}, state, gold, gold_source, gold_strength, weight,
 live_answer, origin, run_id, group, split}.

Gold is the outcome-consistent label, from the strongest outcome that says what the answer should have been
(strong > medium > weak, then newest):
- an outcome naming the right answer (detail.right_answer, as a human label does) gives that answer;
- a "correct" verdict gives the live answer;
- a "wrong" verdict gives the other label of a two-label question (unknown when there are more labels);
- anything else (interrupted, used, ...) is not a verdict and gives no gold.
A "wrong" verdict on a question with more labels still says which answer was NOT right: the example records it as
not_label (a negative label for training, and an answer a student must avoid).
Weak outcomes count only when asked for (min_strength="weak").

The split holds out whole groups. A census reruns the same goals, so a run's group is its goal text
(normalised) when one of its decisions recorded the goal, else the run id: every run of one template lands on
the same side, and no step of a held-out run leaks into training.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional

try:
    from Vera.vera.minds import judge_core as JC, ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import judge_core as JC, ledger_core as LC  # type: ignore

DATASET_ID = "vera.decide"
EXAMPLE_SCHEMA = "decide.example/v1"
WEIGHTS = {"strong": 1.0, "medium": 0.6, "weak": 0.3}
HOLDOUT = 0.2
ORIGIN_FILTERS = ("all", "non_census", "census")


def _strength_rank(s: Any) -> int:
    return LC.STRENGTHS.index(s) if s in LC.STRENGTHS else -1


def settle(live: Dict[str, Any], outcomes: Iterable[Dict[str, Any]], min_strength: str = "medium"
           ) -> Optional[Dict[str, Any]]:
    """What the strongest settling outcome says: {label, not_label, source, strength}. label is the gold answer when
    it is known; not_label is the live answer when the outcome says only that it was wrong (a question with more than
    two labels and no named right answer) - a student repeating it is wrong, any other answer is unknown. None when
    no outcome settles the decision."""
    labels = [str(x) for x in (live.get("labels") or [])]
    answer = live.get("answer")
    floor = _strength_rank(min_strength if min_strength in LC.STRENGTHS else "medium")
    ranked = sorted((o for o in outcomes or [] if _strength_rank(o.get("strength")) >= floor),
                    key=lambda o: (_strength_rank(o.get("strength")), str(o.get("ts") or "")), reverse=True)
    for o in ranked:
        right = (o.get("detail") or {}).get("right_answer")
        label = not_label = None
        if right not in (None, "") and (not labels or str(right) in labels):
            label = str(right)
        elif o.get("label") == "correct" and answer not in (None, ""):
            label = answer
        elif o.get("label") == "wrong" and len(labels) == 2 and str(answer) in labels:
            label = next(x for x in labels if x != str(answer))
        elif o.get("label") == "wrong" and answer not in (None, ""):
            not_label = answer
        else:
            continue
        return {"label": label, "not_label": not_label, "source": o.get("source"), "strength": o.get("strength")}
    return None


def gold(live: Dict[str, Any], outcomes: Iterable[Dict[str, Any]], min_strength: str = "medium"
         ) -> Optional[Dict[str, Any]]:
    """{label, source, strength} when the settling outcome gives the right answer, else None."""
    s = settle(live, outcomes, min_strength)
    if not s or s["label"] is None:
        return None
    return {"label": s["label"], "source": s["source"], "strength": s["strength"]}


def normalise_goal(goal: Any) -> str:
    return re.sub(r"\s+", " ", str(goal or "")).strip().lower()


def group_key(goal: Any, run_id: Any, decision_id: Any = "") -> str:
    g = normalise_goal(goal)
    if g:
        return "goal:" + hashlib.sha256(g.encode("utf-8")).hexdigest()[:16]
    if run_id:
        return f"run:{run_id}"
    return f"decision:{decision_id}"


def split(group: str, holdout: float = HOLDOUT) -> str:
    """test for a stable `holdout` share of the groups, train for the rest."""
    h = int(hashlib.sha256(str(group).encode("utf-8")).hexdigest()[:8], 16) / 0x100000000
    return "test" if h < max(0.0, min(1.0, float(holdout))) else "train"


def origin_ok(origin: Any, origin_filter: str) -> bool:
    if origin_filter in ("", "all"):
        return True
    if origin_filter == "non_census":
        return origin != "census"
    return origin == origin_filter


def question(live: Dict[str, Any]) -> Dict[str, Any]:
    qid = live.get("question_id")
    return {"id": qid, "type": live.get("qtype"), "options": [str(x) for x in (live.get("labels") or [])],
            "instructions": (JC.QUESTIONS.get(qid) or {}).get("system", "")}


def example(live: Dict[str, Any], state: Dict[str, Any], outcomes: Iterable[Dict[str, Any]], goal: Any = "",
            min_strength: str = "medium", holdout: float = HOLDOUT) -> Dict[str, Any]:
    s = settle(live, outcomes, min_strength)
    g = gold(live, outcomes, min_strength)
    grp = group_key(goal, live.get("run_id"), live.get("decision_id"))
    return {"schema": EXAMPLE_SCHEMA, "id": live.get("decision_id"), "question": question(live),
            "state": state or {}, "gold": g["label"] if g else None,
            "gold_source": g["source"] if g else None, "gold_strength": g["strength"] if g else None,
            "weight": WEIGHTS.get(g["strength"], 0.0) if g else 0.0,
            "not_label": s["not_label"] if s else None,
            "live_answer": live.get("answer"), "origin": live.get("origin"), "run_id": live.get("run_id"),
            "ts": live.get("ts"), "group": grp, "split": split(grp, holdout)}


def run_goals(states: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """run_id -> goal, from any decision of the run that recorded one (rows: {run_id, state})."""
    out: Dict[str, str] = {}
    for r in states:
        goal = (r.get("state") or {}).get("goal")
        if r.get("run_id") and goal and r["run_id"] not in out:
            out[r["run_id"]] = str(goal)
    return out


def counts(examples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per question: examples, gold, known_wrong (not_label only), by split (gold only), by gold source, by origin;
    and the gold label mix."""
    out: Dict[str, Any] = {}
    for e in examples:
        q = out.setdefault(e["question"]["id"], {"examples": 0, "gold": 0, "known_wrong": 0, "train": 0, "test": 0,
                                                 "by_source": {}, "by_origin": {}, "labels": {}})
        q["examples"] += 1
        q["by_origin"][e["origin"]] = q["by_origin"].get(e["origin"], 0) + 1
        if e.get("not_label") is not None:
            q["known_wrong"] += 1
        if e["gold"] is not None:
            q["gold"] += 1
            q[e["split"]] += 1
            q["by_source"][e["gold_source"]] = q["by_source"].get(e["gold_source"], 0) + 1
            q["labels"][str(e["gold"])] = q["labels"].get(str(e["gold"]), 0) + 1
    return out


def to_jsonl(examples: Iterable[Dict[str, Any]]) -> str:
    """One example per line, ordered by id, keys sorted: the same set always gives the same bytes."""
    rows = sorted(examples, key=lambda e: str(e.get("id")))
    return "".join(json.dumps(e, sort_keys=True, ensure_ascii=False, default=str) + "\n" for e in rows)
