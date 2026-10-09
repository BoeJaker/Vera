"""A tool shortlist from what similar past steps used (four-minds FM-3 / caps.shortlist).

Measured 2026-10-09 on 342 census steps (step title -> the tools it called successfully, out of a
catalogue of 2,821 capabilities): description retrieval failed (caps.search hit@5 0.076, MiniLM cosine
over descriptions 0.111), while a vote of the 15 most similar PAST steps from other goals, weighted by
similarity and filled with the usage prior, reached hit@5 0.912, recall@10 0.972, MRR 0.668.

So the shortlist is kNN over an index of past loop steps (title, MiniLM vector, tools used), built
nightly from run traces. Each finished step is recorded as a `loop.caps_shortlist` ranking decision with
the tools it actually used as outcomes at their ranks, so the shortlist is measured on live runs before
anything reads it.

Pure: no I/O.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.minds import ledger_core as LC  # type: ignore

QUESTION = "loop.caps_shortlist"
NEIGHBOURS = 15
K = 10


def steps_from_trace(trace: Dict[str, Any]) -> List[Tuple[Any, str, List[str]]]:
    """(step id, title, tools it called successfully) for each executed step of a run trace."""
    out = []
    for s in (trace or {}).get("steps") or []:
        title = str(s.get("title") or "").strip()
        used = sorted({c.get("tool") for c in (s.get("calls") or []) if c.get("ok") and c.get("tool")})
        if title and used:
            out.append((s.get("step_id"), title, used))
    return out


def _unit(v: Sequence[float]) -> List[float]:
    n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / n for x in v]


def shortlist(qvec: Sequence[float], index: Iterable[Dict[str, Any]], k: int = K, neighbours: int = NEIGHBOURS,
              exclude_run: str = "") -> List[Dict[str, Any]]:
    """[{name, score}] best first: the similarity-weighted vote of the nearest indexed steps, then the
    usage prior (how often each tool was used across the index) for the rest of the list. Index rows:
    {run, vec (unit), tools}. Rows of `exclude_run` are skipped."""
    q = _unit(qvec) if qvec else []
    rows = [r for r in index if r.get("run") != exclude_run and r.get("vec")]
    prior = Counter(t for r in rows for t in (r.get("tools") or []))
    vote: Counter = Counter()
    if q:
        sims = sorted(((sum(a * b for a, b in zip(q, r["vec"])), r) for r in rows if len(r["vec"]) == len(q)),
                      key=lambda x: -x[0])[:max(1, int(neighbours))]
        for s, r in sims:
            for t in r.get("tools") or []:
                vote[t] += max(0.0, s)
    ranked: List[Dict[str, Any]] = [{"name": t, "score": round(v, 4), "by": "neighbours"} for t, v in vote.most_common()]
    seen = {x["name"] for x in ranked}
    total = sum(prior.values()) or 1
    ranked += [{"name": t, "score": round(c / total, 4), "by": "prior"} for t, c in prior.most_common() if t not in seen]
    return ranked[:max(1, int(k))]


def decision(names: List[str], *, run_id: str, step_id: Any, origin: str = "user",
             minds_digest: str = "default") -> Dict[str, Any]:
    return LC.make_decision(
        QUESTION, [str(n)[:80] for n in names][:K], origin=origin, surface="loop", qtype="ranking", labels=[],
        state={"run": run_id, "step": step_id}, state_ref={"kind": "loop_step", "id": f"{str(run_id)[:80]}:{step_id}"},
        decider="student", implementation="knn:steps+prior", mode="shadow", minds_digest=minds_digest,
        run_id=str(run_id)[:80], session_id=str(run_id)[:80], detail={"k": len(names)})


def outcomes(d: Dict[str, Any], used: Iterable[str]) -> List[Dict[str, Any]]:
    """One outcome per tool the step used: `used` at its rank, or `missed` when the shortlist lacked it."""
    pos = {n: i + 1 for i, n in enumerate(d.get("answer") or [])}
    out = []
    for t in sorted(set(used or [])):
        if t in pos:
            out.append(LC.make_outcome(d["decision_id"], "used", "step.tools", "strong", detail={"rank": pos[t], "tool": t[:60]}))
        else:
            out.append(LC.make_outcome(d["decision_id"], "missed", "step.tools", "strong", detail={"tool": t[:60]}))
    return out


def summarize(rows: List[Tuple[List[int], int]]) -> Dict[str, Any]:
    """rows: (ranks of used tools found, tools used). hit@5, recall@10, MRR."""
    n = len(rows)
    if not n:
        return {"n": 0}
    hit5 = sum(1 for ranks, _ in rows if any(r <= 5 for r in ranks)) / n
    rec10 = sum(len([r for r in ranks if r <= 10]) / max(1, used) for ranks, used in rows) / n
    mrr = sum((1.0 / min(ranks)) if ranks else 0.0 for ranks, _ in rows) / n
    return {"n": n, "hit@5": round(hit5, 3), "recall@10": round(rec10, 3), "mrr": round(mrr, 3)}
