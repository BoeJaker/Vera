"""Precedent for the planner (four-minds FM-4.2; see precedent_core).

plan.precedent.index keeps one row per past loop run (goal, MiniLM vector, a compact step list with the tools that
worked, outcome, origin) in Postgres `run_precedent_index`, from evolve.runs and the run traces - embeddings only,
no LLM; a night job adds new runs (prod only, in the night window, capped). plan.precedent finds the past runs
nearest a goal ON AN NLP NODE (POST /decide, FM-9.1; the host only votes over what comes back) and returns the
block the planner reads. The loop calls it only when the switch `plan.precedent` is on (default off).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import precedent_core as PC, decide_dispatch as DD, judge_core as JC
except Exception:                        # worktree / app-free import
    from vera.minds import precedent_core as PC, decide_dispatch as DD, judge_core as JC  # type: ignore

log = logging.getLogger("vera.minds.precedent")
KEY_NIGHT = "vera:plan:precedent:night:"
_INDEX: Dict[str, Any] = {"rows": [], "loaded": 0.0, "version": ""}
INDEX_TTL_S = 3600
MAX_ROWS = 20000
_DDL = ("""CREATE TABLE IF NOT EXISTS run_precedent_index (
             run TEXT PRIMARY KEY, goal TEXT NOT NULL, vec JSONB NOT NULL, payload JSONB NOT NULL,
             ts TIMESTAMPTZ NOT NULL DEFAULT NOW())""",)


async def _raw(name: str, /, timeout: float = 60.0, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not available"}
    try:
        import asyncio
        out = await asyncio.wait_for(fn(**kw), timeout)
        return out if isinstance(out, dict) else {}
    except Exception as e:
        return {"error": str(e)}


async def _rows() -> List[Dict[str, Any]]:
    if _INDEX["rows"] and time.time() - _INDEX["loaded"] < INDEX_TTL_S:
        return _INDEX["rows"]
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return _INDEX["rows"]
    async with pool.acquire() as conn:
        for s in _DDL:
            await conn.execute(s)
        recs = await conn.fetch("SELECT run, vec, payload FROM run_precedent_index ORDER BY ts DESC LIMIT $1", MAX_ROWS)
    rows = [{"run": r["run"], "vec": json.loads(r["vec"]) if isinstance(r["vec"], str) else r["vec"],
             "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]} for r in recs]
    _INDEX.update(rows=rows, loaded=time.time(), version=DD.index_version([r["run"] for r in rows]))
    return rows


async def _push_rows() -> List[Dict[str, Any]]:
    return [{"id": r["run"], "vec": r["vec"], "run": r["run"], "payload": r["payload"]} for r in _INDEX["rows"]]


async def build_index(limit_runs: int = 400) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    runs = (await _raw("evolve.runs", 60.0, source="all", limit=int(limit_runs))).get("runs") or []
    async with pool.acquire() as conn:
        for s in _DDL:
            await conn.execute(s)
        done = {r["run"] for r in await conn.fetch("SELECT run FROM run_precedent_index")}
    recs = []
    for r in runs:
        sid = str(r.get("run_id") or "")
        if not sid or sid in done or sid.startswith("chat:"):
            continue
        rec = PC.run_record(r, await _raw("workshop.agent_loop.trace", 60.0, session_id=sid))
        if rec:
            recs.append(rec)
    added = 0
    for i in range(0, len(recs), 64):
        batch = recs[i:i + 64]
        emb = await _raw("nlp.embed", 60.0, texts=[x["goal"] for x in batch])
        vecs = emb.get("embeddings") if isinstance(emb.get("embeddings"), list) else []
        if len(vecs) != len(batch):
            continue
        async with pool.acquire() as conn:
            for rec, vec in zip(batch, vecs):
                await conn.execute(
                    "INSERT INTO run_precedent_index (run, goal, vec, payload) VALUES ($1, $2, $3::jsonb, $4::jsonb) "
                    "ON CONFLICT (run) DO NOTHING", rec["run"], rec["goal"],
                    json.dumps([round(float(x), 5) for x in vec]), json.dumps(rec))
                added += 1
    _INDEX["loaded"] = 0.0
    return {"ok": True, "runs_seen": len(runs), "indexed": added}


@capability(
    "plan.precedent", http_method="POST", http_path="/plan/precedent", http_tags=["minds", "plan"],
    memory="off", silent=True,
    description="Precedent for the planner (four-minds FM-4.2): the past loop runs nearest a goal (MiniLM, searched on "
                "an NLP node), up to 2 that went well and 1 warning from one that did not, above a similarity floor; "
                "census runs never get census precedents of the same goal. Input: goal (str!), origin (str, the "
                "asking run's origin), exclude_run (str), floor (float, default 0.6). Output: {precedents: [{run, "
                "goal, outcome, similarity, steps}], block (the text the planner reads), node, index_rows}.",
)
async def cap_plan_precedent(goal: str = "", origin: str = "user", exclude_run: str = "", floor: float = PC.FLOOR,
                             trace_id=None) -> Dict[str, Any]:
    if not str(goal or "").strip():
        return {"error": "goal is required"}
    rows = await _rows()
    if not rows:
        return {"precedents": [], "block": "", "index_rows": 0}
    res = await DD.knn("run_goals", _INDEX["version"], _push_rows, text=str(goal)[:1000], k=8,
                       exclude_run=exclude_run or "")
    if not res.get("ok"):
        return {"precedents": [], "block": "", "index_rows": len(rows), "error": res.get("error")}
    chosen = PC.choose([(n["score"], n.get("payload") or {}) for n in res.get("neighbours") or []], goal,
                       origin or "user", exclude_run, float(floor or PC.FLOOR))
    return {"precedents": [{k: p.get(k) for k in ("run", "goal", "outcome", "similarity", "steps_n", "origin")}
                           for p in chosen],
            "block": PC.render(chosen), "node": res.get("node"), "index_rows": len(rows)}


@capability(
    "plan.precedent.index", http_method="POST", http_path="/plan/precedent/index", http_tags=["minds", "plan"],
    memory="off",
    description="Index recent loop runs for plan.precedent (goal vector, compact steps, outcome, origin; embeddings "
                "only, no LLM). Runs nightly by itself; use this for a first fill. Input: limit_runs (int, default "
                "400). Output: {ok, runs_seen, indexed}.",
)
async def cap_plan_precedent_index(limit_runs: int = 400, trace_id=None) -> Dict[str, Any]:
    return await build_index(limit_runs=limit_runs)


async def _night_job() -> None:
    """Index the day's new runs once a night. Prod only; the night key is set after the build succeeded."""
    try:
        if getattr(_orch, "_IS_WORKER", False):
            return
        now = datetime.now()
        if not JC.in_window(now.hour):
            return
        night = now.date().isoformat() if now.hour >= 12 else datetime.fromordinal(now.toordinal() - 1).date().isoformat()
        r = getattr(_orch, "REDIS", None)
        if r is None or await r.get(KEY_NIGHT + night):
            return
        out = await build_index()
        if out.get("ok"):
            await r.set(KEY_NIGHT + night, "1", ex=86400)
        log.info("plan.precedent nightly index %s: %s", night, out)
    except Exception as e:
        log.warning("plan precedent night job: %s", e)


# On the Loop Lab schedule, not an in-code timer (owner, 18 Oct 2026: "make sure you are making use of the schedule system and not creating ad-hoc timing rules"). Its entry calls plan.precedent.index.
