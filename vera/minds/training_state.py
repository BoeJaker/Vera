"""The training store: what each recorded decision saw (four-minds FM-0.2; see training_state_core).

put() is called beside a ledger append and never makes the caller wait: the row goes to Postgres
(`decision_states`, keyed by decision_id) in a background task, and a failure is logged and
dropped. Only enabled surfaces are stored (VERA_TRAINING_STATE_SURFACES, default "loop").

A daily job deletes rows past the 90-day retention. decide.training_state shows what is kept;
decide.purge deletes by surface, session or date.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

# ONE module object (as data_fabric, 11 Oct 2026): the loader loads this file as `training_state`, while loop_ledger and
# discovery import `Vera.vera.minds.training_state` - a second copy with its own stats, its own decide.* capabilities
# registered over the first, and its own retention job. Both names point at this object before anything else runs.
for _alias in ("training_state", "Vera.vera.minds.training_state"):
    sys.modules.setdefault(_alias, sys.modules[__name__])
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import training_state_core as TS
except Exception:                        # worktree / app-free import
    from vera.minds import training_state_core as TS   # type: ignore

log = logging.getLogger("vera.minds.training_state")
_PENDING: set = set()
_DDL_DONE = False
_STATS: Dict[str, int] = {"stored": 0, "skipped_surface": 0, "dropped_no_db": 0, "errors": 0}
_DDL = (
    """CREATE TABLE IF NOT EXISTS decision_states (
           decision_id TEXT PRIMARY KEY,
           question_id TEXT NOT NULL,
           surface     TEXT NOT NULL,
           session_id  TEXT,
           state       JSONB NOT NULL,
           ts          TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS decision_states_q_ts ON decision_states (question_id, ts)",
)


def _cred():
    try:
        from Vera.vera.log_setup import redact
        return redact
    except Exception:
        return None


def surfaces():
    return TS.enabled_surfaces(os.environ.get("VERA_TRAINING_STATE_SURFACES"))


# Decisions about a whole goal also keep the goal's Frame (sense.frame: domains, patterns), the features an
# intent/tier student learns from (four-minds FM-1.3 -> student state). Fetched in the background.
FRAME_QUESTIONS = ("loop.intent", "loop.tier")
FRAME_TIMEOUT_S = 5.0
FRAME_KEYS = ("domain", "domains", "patterns", "degraded", "cards")


def put_framed(decision: Optional[Dict[str, Any]], inputs: Optional[Dict[str, Any]]) -> None:
    """put(), with the goal's Frame added to the inputs first, all in the background. Never raises."""
    try:
        if not decision or not inputs or not TS.wanted(decision, surfaces()):
            put(decision, inputs)
            return
        task = asyncio.get_running_loop().create_task(_framed(decision, dict(inputs)))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("training state frame: %s", e)


async def _framed(decision: Dict[str, Any], inputs: Dict[str, Any]) -> None:
    fr = None
    try:
        from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
        cap = CAPABILITY_REGISTRY.get("sense.frame") or {}
        fn = cap.get("raw") or cap.get("func")
        if fn and str(inputs.get("goal") or "").strip():
            out = await asyncio.wait_for(fn(text=str(inputs["goal"]), kind="goal"), FRAME_TIMEOUT_S)
            if isinstance(out, dict) and not out.get("error"):
                fr = {k: out.get(k) for k in FRAME_KEYS}
    except Exception as e:
        log.debug("training state frame fetch: %s", e)
    put(decision, dict(inputs, frame=fr) if fr else inputs)


def put(decision: Optional[Dict[str, Any]], inputs: Optional[Dict[str, Any]]) -> None:
    """Keep what this decision saw, in the background. Never raises."""
    try:
        if not decision or not inputs:
            return
        if not TS.wanted(decision, surfaces()):
            _STATS["skipped_surface"] += 1
            return
        row = TS.state_row(decision, TS.render(inputs, _cred()))
        task = asyncio.get_running_loop().create_task(_write(row))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("training state: %s", e)


async def put_now(decision: Optional[Dict[str, Any]], inputs: Optional[Dict[str, Any]]) -> bool:
    """put(), awaited: for a backfill that writes many rows one after another instead of a task per row.
    True when the row was written (or already there). Never raises."""
    try:
        if not decision or not inputs or not TS.wanted(decision, surfaces()):
            return False
        before = _STATS["stored"]
        await _write(TS.state_row(decision, TS.render(inputs, _cred())))
        return _STATS["stored"] > before
    except Exception as e:
        log.debug("training state: %s", e)
        return False


async def _ensure(conn) -> None:
    global _DDL_DONE
    if _DDL_DONE:
        return
    for stmt in _DDL:
        await conn.execute(stmt)
    _DDL_DONE = True


async def _write(row: Dict[str, Any]) -> None:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        _STATS["dropped_no_db"] += 1
        return
    try:
        async with pool.acquire() as conn:
            await _ensure(conn)
            await conn.execute(
                "INSERT INTO decision_states (decision_id, question_id, surface, session_id, state) "
                "VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT (decision_id) DO NOTHING",
                row["decision_id"], row["question_id"] or "", row["surface"], row["session_id"],
                json.dumps(row["state"], default=str))
        _STATS["stored"] += 1
    except Exception as e:
        _STATS["errors"] += 1
        log.debug("training state write: %s", e)


async def _retention_job() -> None:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await _ensure(conn)
            await conn.execute("DELETE FROM decision_states WHERE ts < NOW() - make_interval(days => $1)",
                               TS.RETENTION_DAYS)
    except Exception as e:
        log.warning("training state retention: %s", e)


schedule(_retention_job, interval=86400, name="decide_training_state_retention", singleton=True,
         skip_in_sandbox=True)


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "decide.training_state", http_method="GET", http_path="/decide/training_state", http_tags=["minds"],
    contract=_MC.contract("decision.inputs.read", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="The training store (four-minds FM-0.2): what recorded decisions saw, kept so decision "
                "models can learn them - redacted, bounded, 90 days, only for enabled surfaces "
                "(VERA_TRAINING_STATE_SURFACES, default loop). Input: question_id (str, optional), "
                "samples (int, default 3, max 20). Output: {surfaces, retention_days, stats, by_question: "
                "[{question_id, n, with_outcome}], samples: [{decision_id, question_id, answer, state, "
                "outcomes}]}.",
)
async def cap_decide_training_state(question_id: str = "", samples: int = 3, trace_id=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"surfaces": sorted(surfaces()), "retention_days": TS.RETENTION_DAYS,
                           "stats": dict(_STATS)}
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        out["error"] = "postgres unavailable"
        return out
    try:
        samples = max(0, min(20, int(samples)))
    except Exception:
        samples = 3
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch(
            """SELECT s.question_id, count(*) AS n,
                      count(*) FILTER (WHERE EXISTS (SELECT 1 FROM decision_outcomes o
                                                     WHERE o.decision_id = s.decision_id)) AS with_outcome
               FROM decision_states s GROUP BY s.question_id ORDER BY n DESC""")
        out["by_question"] = [dict(r) for r in rows]
        if samples:
            srows = await conn.fetch(
                """SELECT s.decision_id, s.question_id, s.state, l.row->'answer' AS answer,
                          (SELECT json_agg(json_build_object('label', o.row->>'label', 'source', o.source))
                             FROM decision_outcomes o WHERE o.decision_id = s.decision_id) AS outcomes
                   FROM decision_states s LEFT JOIN decision_ledger l ON l.decision_id = s.decision_id
                   WHERE ($1 = '' OR s.question_id = $1) ORDER BY s.ts DESC LIMIT $2""",
                str(question_id or ""), samples)
            out["samples"] = [{"decision_id": r["decision_id"], "question_id": r["question_id"],
                               "answer": json.loads(r["answer"]) if isinstance(r["answer"], str) else r["answer"],
                               "state": json.loads(r["state"]) if isinstance(r["state"], str) else r["state"],
                               "outcomes": json.loads(r["outcomes"]) if isinstance(r["outcomes"], str)
                               else r["outcomes"]} for r in srows]
    return out


@capability(
    "decide.purge", http_method="POST", http_path="/decide/purge", http_tags=["minds"],
    contract=_MC.contract("decision.inputs.purge", effects=["delete"], idempotency="idempotent", approval="confirm_argument"),
    memory="off",
    description="Delete stored decision inputs from the training store (four-minds FM-0.2). Input: surface, "
                "session_id (str), before (ISO date) - at least one; confirm (bool, default false: count "
                "only). The ledger rows themselves are kept (they hold only hashes). Output: {ok, matched, "
                "deleted}.",
)
async def cap_decide_purge(surface: str = "", session_id: str = "", before: str = "", confirm: bool = False,
                           trace_id=None) -> Dict[str, Any]:
    if isinstance(confirm, str):
        confirm = confirm.strip().lower() in ("1", "true", "yes")
    where, params = [], []
    if str(surface or "").strip():
        params.append(str(surface).strip()); where.append(f"surface = ${len(params)}")
    if str(session_id or "").strip():
        params.append(str(session_id).strip()); where.append(f"session_id = ${len(params)}")
    if str(before or "").strip():
        try:
            d = datetime.fromisoformat(str(before).strip().replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": "before is not an ISO date"}
        params.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc)); where.append(f"ts < ${len(params)}")
    if not where:
        return {"ok": False, "error": "pass surface, session_id or before (nothing is purged wholesale)"}
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    cond = " AND ".join(where)
    async with pool.acquire() as conn:
        await _ensure(conn)
        matched = await conn.fetchval(f"SELECT count(*) FROM decision_states WHERE {cond}", *params)
        deleted = 0
        if confirm and matched:
            res = await conn.execute(f"DELETE FROM decision_states WHERE {cond}", *params)
            deleted = int(str(res).split()[-1]) if res else 0
    return {"ok": True, "matched": int(matched or 0), "deleted": deleted, "confirm": bool(confirm)}
