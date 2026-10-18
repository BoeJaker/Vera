"""The labelling page: a person marks recorded decisions right or wrong (see label_core).

decide.label.queue lists decisions that have their inputs in the training store and no human
label yet, disagreements with a shadow first. decide.label records the person's verdict as an
Outcome v0 row (source human.label, strong) through the ledger's normal write path. The page
is served at /decide/labels.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, register_ui

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import label_core as LB
except Exception:                        # worktree / app-free import
    from vera.minds import label_core as LB   # type: ignore

log = logging.getLogger("vera.minds.labels")
_HERE = Path(__file__).parent


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "decide.label.queue", http_method="GET", http_path="/decide/label/queue", http_tags=["minds"],
    contract=_MC.contract("decision.label.queue", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Decisions waiting for a person's label (four-minds FM-7.0 labelling view): ones whose "
                "inputs are in the training store and that have no human label yet, those where a shadow "
                "(e.g. the zero-shot intent) disagreed with the live answer first. Input: question_id (str, "
                "optional), limit (int, default 20, max 100). Output: {items: [{decision_id, question_id, "
                "labels, answer, state, shadows, outcomes, disagreement}], questions: [{question_id, waiting}]}.",
)
async def cap_decide_label_queue(question_id: str = "", limit: int = 20, trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"items": [], "error": "postgres unavailable"}
    try:
        limit = max(1, min(100, int(limit)))
    except Exception:
        limit = 20
    q = str(question_id or "")
    unlabelled = ("NOT EXISTS (SELECT 1 FROM decision_outcomes o WHERE o.decision_id = s.decision_id "
                  "AND o.source = '" + LB.SOURCE + "')")
    async with pool.acquire() as conn:
        counts = await conn.fetch(
            f"""SELECT s.question_id, count(*) AS waiting FROM decision_states s
                JOIN decision_ledger l ON l.decision_id = s.decision_id
                WHERE COALESCE(l.mode, '') <> 'shadow' AND {unlabelled}
                GROUP BY s.question_id ORDER BY waiting DESC""")
        rows = await conn.fetch(
            f"""SELECT s.decision_id, s.state, s.ts, l.row, l.run_id, l.question_id
                FROM decision_states s JOIN decision_ledger l ON l.decision_id = s.decision_id
                WHERE ($1 = '' OR s.question_id = $1) AND COALESCE(l.mode, '') <> 'shadow' AND {unlabelled}
                ORDER BY s.ts DESC LIMIT $2""", q, limit * 4)
        ids = [r["decision_id"] for r in rows]
        runs = sorted({r["run_id"] for r in rows if r["run_id"]})
        srows: List[Dict[str, Any]] = []
        if ids:
            # each shadow by the decision it answered (detail.for); rows without it by their run (older rows)
            for s in await conn.fetch(
                    """SELECT run_id, question_id, row FROM decision_ledger
                       WHERE mode = 'shadow' AND (row->'detail'->>'for' = ANY($1::text[])
                             OR (run_id = ANY($2::text[]) AND COALESCE(row->'detail'->>'for', '') = ''))""",
                    ids, runs):
                srows.append({"run_id": s["run_id"], "question_id": s["question_id"], "row": _json(s["row"])})
        shadows = LB.shadows_for([{"decision_id": r["decision_id"], "run_id": r["run_id"],
                                   "question_id": r["question_id"]} for r in rows], srows)
        outs: Dict[str, List[Dict[str, Any]]] = {}
        if ids:
            for o in await conn.fetch(
                    """SELECT decision_id, source, strength, label FROM decision_outcomes
                       WHERE decision_id = ANY($1::text[])""", ids):
                outs.setdefault(o["decision_id"], []).append(
                    {"source": o["source"], "strength": o["strength"], "label": _json(o["label"])})
    items = [{"row": _json(r["row"]), "state": _json(r["state"]), "ts": r["ts"].isoformat() if r["ts"] else "",
              "shadows": shadows.get(r["decision_id"], []),
              "outcomes": outs.get(r["decision_id"], [])} for r in rows]
    return {"items": [LB.view(it) for it in LB.order(items, limit)],
            "questions": [dict(c) for c in counts]}


@capability(
    "decide.label", http_method="POST", http_path="/decide/label", http_tags=["minds"],
    memory="off",
    description="Record a person's label for one recorded decision (four-minds FM-7.0): verdict correct | "
                "wrong, or right_answer (one of the decision's labels; the verdict follows from it). Written "
                "as an Outcome v0 row (source human.label, strength strong) through the ledger. Input: "
                "decision_id, verdict, right_answer. Output: {ok, label} or {ok:false, error}.",
)
async def cap_decide_label(decision_id: str = "", verdict: str = "", right_answer: str = "",
                           trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT row FROM decision_ledger WHERE decision_id = $1 LIMIT 1",
                                  str(decision_id or ""))
    row = LB.parse_row(raw)
    if not row:
        return {"ok": False, "error": f"no decision {decision_id!r}"}
    try:
        outcome = LB.human_outcome(row, verdict=str(verdict or ""), right_answer=right_answer or None)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:                    # pragma: no cover
        from vera.minds import ledger_capabilities as L  # type: ignore
    res = await L.append(outcome)
    if not res.get("ok"):
        return {"ok": False, "error": "; ".join(res.get("errors") or ["the ledger refused the row"])}
    return {"ok": True, "label": outcome["label"], "right_answer": (outcome.get("detail") or {}).get("right_answer")}


@APP.get("/decide/labels", include_in_schema=False)
async def _labels_page():
    from fastapi.responses import HTMLResponse
    p = _HERE / "labels_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<pre>labels_panel.html not found</pre>", status_code=404)


# D10-4: the labelling page from the panels menu (an inject, not a top-level tab), in its own frame
register_ui(
    "decision-labels",
    "Decision labels",
    "\u2713",
    """<div id="decision-labels-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/decide/labels" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["decide.label.queue", "decide.label"],
    mode="inject",
    tab_order=88,
)
