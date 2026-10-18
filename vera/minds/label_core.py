"""Human labels for recorded decisions (four-minds FM-7.0's labelling view, used for every question).

A person marking a decision right or wrong - or naming the answer it should have given - is the
strongest outcome a decision can have. It is an Outcome v0 row like any other (source
`human.label`, strength strong), so calibration and student training read it with no special case.

The queue shows the most useful decisions first: those where an independent labeller (a shadow,
such as the zero-shot intent) disagreed with the live answer, then the newest.

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

SOURCE = "human.label"
VERDICTS = ("correct", "wrong")


def human_outcome(decision: Dict[str, Any], verdict: str = "", right_answer: Any = None,
                  by: str = "owner") -> Dict[str, Any]:
    """The outcome a person gives a decision. With `right_answer` the verdict follows from it
    (it must be one of the decision's labels, when it has labels); otherwise `verdict` is
    correct | wrong. Raises ValueError on anything else."""
    labels = [str(x) for x in (decision.get("labels") or [])]
    detail: Dict[str, Any] = {"by": str(by or "owner")[:40]}
    if right_answer not in (None, ""):
        ra = str(right_answer)
        if labels and ra not in labels:
            raise ValueError(f"right_answer {ra!r} is not one of {labels}")
        verdict = "correct" if ra == str(decision.get("answer")) else "wrong"
        detail["right_answer"] = ra[:80]
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {list(VERDICTS)} (or give right_answer)")
    return LC.make_outcome(decision["decision_id"], verdict, SOURCE, "strong", detail=detail)


def disagrees(live: Dict[str, Any], shadows: Iterable[Dict[str, Any]]) -> bool:
    return any(str(s.get("answer")) != str(live.get("answer")) for s in shadows or [])


def order(items: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, Any]]:
    """Disagreements first, then newest first (items carry "row", "shadows", "ts")."""
    newest = sorted(items, key=lambda it: str(it.get("ts") or ""), reverse=True)
    ranked = sorted(newest, key=lambda it: 0 if disagrees(it.get("row") or {}, it.get("shadows") or []) else 1)
    return ranked[:max(0, int(limit))]


def view(item: Dict[str, Any]) -> Dict[str, Any]:
    """What the page shows for one decision."""
    row = item.get("row") or {}
    return {"decision_id": row.get("decision_id"), "question_id": row.get("question_id"),
            "labels": row.get("labels") or [], "answer": row.get("answer"), "decider": row.get("decider"),
            "implementation": row.get("implementation"), "origin": row.get("origin"),
            "ts": str(item.get("ts") or ""), "state": item.get("state") or {},
            "shadows": [{"answer": s.get("answer"), "implementation": s.get("implementation"), "p": s.get("p")}
                        for s in item.get("shadows") or []],
            "outcomes": item.get("outcomes") or [],
            "disagreement": disagrees(row, item.get("shadows") or [])}


def parse_row(raw: Any) -> Optional[Dict[str, Any]]:
    import json
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def shadows_for(decisions: Iterable[Dict[str, Any]], shadow_rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """{decision_id: [shadow rows]}: a shadow belongs to the decision its detail.for names (D10-4, 18 Oct - the
    queue had grouped shadows by (run, question), so each claim of a run showed every claim's 42 shadows). A shadow
    with no detail.for (older rows) falls back to the decisions of its run and question.
    decisions: {decision_id, run_id, question_id}; shadow_rows: {run_id, question_id, row}."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    by_run: Dict[tuple, List[str]] = {}
    ids = set()
    for d in decisions:
        did = str(d.get("decision_id") or "")
        ids.add(did)
        out.setdefault(did, [])
        by_run.setdefault((d.get("run_id"), d.get("question_id")), []).append(did)
    for s in shadow_rows:
        row = s.get("row") if isinstance(s.get("row"), dict) else {}
        target = str((row.get("detail") or {}).get("for") or "")
        if target:
            if target in ids:
                out[target].append(row)
            continue
        for did in by_run.get((s.get("run_id"), s.get("question_id")), []):
            out[did].append(row)
    return out
