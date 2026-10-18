"""decide.calibration and its weekly snapshot (four-minds FM-11; see calibration_core).

decide.calibration reads a window of the ledger (and the window before it): every shadow answer with its live
decision and outcomes, every live decision with its decider and whether its inputs reached the training store,
and returns calibration per (question, implementation) - node included - plus escalation, drop, orphan and drift
per question, and the FM-2.5 revert triggers as alerts. Read-only.

A weekly job (Sunday, 05:30 local, prod only) keeps the 7-day report in Redis (the last 12) for
decide.calibration.history, so a student's numbers can be followed week by week.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import calibration_core as CC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import calibration_core as CC   # type: ignore
    from vera.minds import contract_core as _MC     # type: ignore

log = logging.getLogger("vera.minds.calibration")
KEY_HISTORY = "vera:decide:calibration:history"
KEY_WEEK = "vera:decide:calibration:week:"
HISTORY_N = 12


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def _window(conn, days: int, offset_days: int):
    shadows = []
    for r in await conn.fetch(
            """SELECT x.question_id, x.row->>'implementation' AS impl, x.answer AS sa, x.p AS sp,
                      x.row->'detail'->>'would_take' AS wt, x.row->'detail'->>'for' AS f, l.row AS live,
                      (SELECT json_agg(o.row) FROM decision_outcomes o WHERE o.decision_id = l.decision_id) AS outs
               FROM decision_ledger x LEFT JOIN decision_ledger l ON l.decision_id = x.row->'detail'->>'for'
               WHERE x.mode = 'shadow'
                 AND x.row->'detail'->>'for' IS NOT NULL
                 AND x.ts >= NOW() - make_interval(days => $1) AND x.ts < NOW() - make_interval(days => $2)""",
            days + offset_days, offset_days):
        live = _j(r["live"])
        outs = [_j(o) for o in (_j(r["outs"]) or [])]
        shadows.append({"question_id": r["question_id"], "impl": r["impl"] or "?", "answer": _j(r["sa"]),
                        "p": r["sp"], "would_take": str(r["wt"]).lower() == "true", "for": r["f"],
                        "live": (live or {}).get("answer") if live else None,
                        "gold": CC.gold_of(live, outs) if live else None})
    live_rows = [{"question_id": r["question_id"], "decision_id": r["decision_id"], "run_id": r["run_id"],
                  "decider": r["decider"], "mode": r["mode"], "answer": _j(r["answer"]),
                  "stored": (r["has_state"] if r["surface"] in ("loop", "discovery") else None)}
                 for r in await conn.fetch(
                     """SELECT l.question_id, l.decision_id, l.run_id, l.decider, l.mode, l.answer, l.surface,
                               EXISTS (SELECT 1 FROM decision_states s WHERE s.decision_id = l.decision_id) AS has_state
                        FROM decision_ledger l WHERE COALESCE(l.mode, '') <> 'shadow'
                          AND l.ts >= NOW() - make_interval(days => $1) AND l.ts < NOW() - make_interval(days => $2)""",
                     days + offset_days, offset_days)]
    return shadows, live_rows


@capability(
    "decide.calibration", http_method="GET", http_path="/decide/calibration", http_tags=["minds"],
    memory="off", silent=True,
    description="How every System 1 student is doing (four-minds FM-11): for the last `days` (default 7) and the "
                "window before, per question and implementation (the node included when a student was pinned to "
                "one): n, agreement with the live answer, accuracy / ECE / precision and coverage at each confidence "
                "against gold, precision at tau, and the co-sign sample's precision; per question: escalation (the "
                "share a student_first student handed to the big model), drop (live decisions whose inputs never "
                "reached the training store), orphans (shadows without their live decision) and drift (the live "
                "answer mix against the previous window); alerts = FM-2.5's revert triggers (precision at tau -3 "
                "points, escalation > 60%) plus ECE > 0.05 and drift, each only with >= 20 rows. Input: days (1-60), "
                "question_id (optional), tau (default 0.95). Read-only. Output: {window, questions, alerts}.",
)
async def cap_decide_calibration(days: int = 7, question_id: str = "", tau: float = 0.95,
                                 trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"error": "postgres unavailable"}
    days = max(1, min(60, int(days or 7)))
    async with pool.acquire() as conn:
        sc, lc = await _window(conn, days, 0)
        sp, lp = await _window(conn, days, days)
    if question_id:
        sc, lc, sp, lp = ([r for r in rows if r["question_id"] == question_id] for rows in (sc, lc, sp, lp))
    out = CC.report(sc, sp, lc, lp, tau=float(tau or 0.95))
    out["window"] = {"days": days, "tau": float(tau or 0.95), "at": datetime.now(timezone.utc).isoformat()}
    return out


@capability(
    "decide.calibration.history", http_method="GET", http_path="/decide/calibration/history",
    http_tags=["minds"], memory="off", silent=True,
    description="The weekly decide.calibration snapshots (the last 12, newest first), as {weeks: [{week, alerts, "
                "questions: {question: {implementation: {n, agreement, precision_at_tau, ece}}}}]}.",
)
async def cap_decide_calibration_history(trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"weeks": [], "error": "redis unavailable"}
    weeks = []
    for raw in await r.lrange(KEY_HISTORY, 0, HISTORY_N - 1):
        try:
            weeks.append(json.loads(raw))
        except Exception:
            continue
    return {"weeks": weeks}


def _digest(rep: Dict[str, Any], week: str) -> Dict[str, Any]:
    qs = {}
    for q, qo in (rep.get("questions") or {}).items():
        qs[q] = {impl: {"n": (v.get("current") or {}).get("n"), "agreement": (v.get("current") or {}).get("agreement"),
                        "precision_at_tau": ((v.get("current") or {}).get("vs_gold") or {}).get("precision_at_tau"),
                        "ece": ((v.get("current") or {}).get("vs_gold") or {}).get("ece")}
                 for impl, v in (qo.get("implementations") or {}).items()}
        qs[q]["_question"] = {"escalation": qo.get("escalation"), "drop_rate": qo.get("drop_rate"),
                              "orphan_rate": qo.get("orphan_rate"), "drift": qo.get("drift")}
    return {"week": week, "alerts": rep.get("alerts") or [], "questions": qs}


async def _weekly_job() -> None:
    """Sunday 05:30-06:30 local, prod only, once a week (the week key is set after the snapshot is stored)."""
    try:
        if getattr(_orch, "_IS_WORKER", False):
            return
        now = datetime.now()
        r = getattr(_orch, "REDIS", None)
        week = now.strftime("%G-W%V")
        if r is None or await r.get(KEY_WEEK + week):
            return
        rep = await cap_decide_calibration(days=7)
        if rep.get("error"):
            log.warning("weekly calibration: %s", rep["error"])
            return
        await r.lpush(KEY_HISTORY, json.dumps(_digest(rep, week)))
        await r.ltrim(KEY_HISTORY, 0, HISTORY_N - 1)
        await r.set(KEY_WEEK + week, "1", ex=8 * 86400)
        log.info("weekly calibration %s: %d questions, %d alerts", week, len(rep.get("questions") or {}),
                 len(rep.get("alerts") or []))
    except Exception as e:
        log.warning("weekly calibration job: %s", e)


# On the Loop Lab schedule, not an in-code timer (owner, 18 Oct 2026: "make sure you are making use of the schedule system and not creating ad-hoc timing rules").


@capability(
    "decide.calibration.weekly", http_method="POST", http_path="/decide/calibration/weekly", http_tags=["minds"],
    contract=_MC.contract("decision.calibration.weekly", effects=["read", "write"], idempotency="idempotent"),
    memory="off",
    description="Store this week's calibration snapshot (four-minds FM-2): decide.calibration over 7 days, digested into "
                "decide.calibration.history; once per ISO week. The target of a Loop Lab schedule entry. Output: {ok, week}.",
)
async def cap_decide_calibration_weekly(trace_id=None) -> Dict[str, Any]:
    await _weekly_job()
    week = datetime.now().strftime("%G-W%V")
    r = getattr(_orch, "REDIS", None)
    return {"ok": bool(r is not None and await r.get(KEY_WEEK + week)), "week": week}
