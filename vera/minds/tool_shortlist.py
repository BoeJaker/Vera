"""caps.shortlist: tools from what similar past steps used (four-minds FM-3; see tool_shortlist_core).

The index (`tool_step_index`, Postgres) holds one row per executed loop step: run, step, title, its
MiniLM vector and the tools it called successfully. It is built from run traces (embeddings only, no
LLM): nightly in the 23:00-06:00 window, or on demand with caps.shortlist.index. Each finished loop step
is recorded in the background as a `loop.caps_shortlist` shadow ranking decision with the tools it really
used as outcomes; caps.shortlist.report reads hit@5 / recall@10 / MRR from them. Nothing reads the
shortlist to change behaviour yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import tool_shortlist_core as TC
    from Vera.vera.minds import judge_core as JC
except Exception:                        # worktree / app-free import
    from vera.minds import tool_shortlist_core as TC   # type: ignore
    from vera.minds import judge_core as JC            # type: ignore
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import decide_dispatch as DD
except Exception:                        # worktree / app-free import
    from vera.minds import decide_dispatch as DD       # type: ignore

log = logging.getLogger("vera.minds.tool_shortlist")
_PENDING: set = set()
_INDEX: Dict[str, Any] = {"rows": [], "loaded": 0.0, "version": "", "prior": None}
INDEX_TTL_S = 3600
MAX_ROWS = 20000
KEY_NIGHT = "vera:caps:shortlist:night:"
_DDL = (
    """CREATE TABLE IF NOT EXISTS tool_step_index (
           run TEXT NOT NULL, step TEXT NOT NULL, title TEXT NOT NULL, vec JSONB NOT NULL,
           tools JSONB NOT NULL, task TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           PRIMARY KEY (run, step))""",
)
_DDL_DONE = False


async def _raw(name: str, /, timeout: float = 30.0, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not available"}
    try:
        out = await asyncio.wait_for(fn(**kw), timeout)
        return out if isinstance(out, dict) else {}
    except Exception as e:
        return {"error": f"{name}: {type(e).__name__}"}


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in _DDL:
            await conn.execute(s)
        _DDL_DONE = True


async def _embed(texts: List[str]) -> List[Optional[List[float]]]:
    out: List[Optional[List[float]]] = []
    for i in range(0, len(texts), 64):
        r = await _raw("nlp.embed", 30.0, texts=texts[i:i + 64])
        vecs = r.get("embeddings") if isinstance(r.get("embeddings"), list) else []
        out += vecs if len(vecs) == len(texts[i:i + 64]) else [None] * len(texts[i:i + 64])
    return out


async def _rows() -> List[Dict[str, Any]]:
    """The index, cached in process for an hour."""
    if _INDEX["rows"] and time.time() - _INDEX["loaded"] < INDEX_TTL_S:
        return _INDEX["rows"]
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return _INDEX["rows"]
    async with pool.acquire() as conn:
        await _ensure(conn)
        recs = await conn.fetch("SELECT run, vec, tools FROM tool_step_index ORDER BY ts DESC LIMIT $1", MAX_ROWS)
    rows = []
    for r in recs:
        vec = json.loads(r["vec"]) if isinstance(r["vec"], str) else r["vec"]
        tools = json.loads(r["tools"]) if isinstance(r["tools"], str) else r["tools"]
        rows.append({"run": r["run"], "vec": vec, "tools": tools})
    _INDEX.update(rows=rows, loaded=time.time(), prior=TC.prior(rows),
                  version=DD.index_version([f"{r['run']}:{len(r['tools'] or [])}" for r in rows]))
    return rows


async def _push_rows() -> List[Dict[str, Any]]:
    """The index as a node keeps it (POST /decide/index): the tools ride as each row's payload."""
    return [{"id": i, "vec": r["vec"], "run": r["run"], "payload": r["tools"]}
            for i, r in enumerate(_INDEX["rows"])]


async def build_index(limit_runs: int = 400) -> Dict[str, Any]:
    """Index the steps of recent runs not indexed yet. Embeddings only."""
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    runs = (await _raw("evolve.runs", 60.0, source="all", limit=int(limit_runs))).get("runs") or []
    async with pool.acquire() as conn:
        await _ensure(conn)
        done = {r["run"] for r in await conn.fetch("SELECT DISTINCT run FROM tool_step_index")}
    new_rows, traced = [], 0
    for r in runs:
        sid = str(r.get("run_id") or "")
        if not sid or sid in done or sid.startswith("chat:"):
            continue
        tr = await _raw("workshop.agent_loop.trace", 60.0, session_id=sid)
        traced += 1
        for step_id, title, used in TC.steps_from_trace(tr):
            new_rows.append((sid, str(step_id), title[:500], used, str(r.get("task") or "")[:120]))
    vecs = await _embed([t for _, _, t, _, _ in new_rows])
    added = 0
    async with pool.acquire() as conn:
        for (sid, step, title, used, task), vec in zip(new_rows, vecs):
            if not vec:
                continue
            await conn.execute(
                "INSERT INTO tool_step_index (run, step, title, vec, tools, task) VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6) "
                "ON CONFLICT (run, step) DO NOTHING", sid, step, title,
                json.dumps([round(float(x), 5) for x in TC._unit(vec)]), json.dumps(used), task)
            added += 1
        await conn.execute("DELETE FROM tool_step_index WHERE ts < NOW() - INTERVAL '120 days'")
    _INDEX["loaded"] = 0.0
    return {"ok": True, "runs_seen": len(runs), "traced": traced, "steps_added": added}


@capability(
    "caps.shortlist", http_method="POST", http_path="/caps/shortlist", http_tags=["minds", "caps"],
    memory="off", silent=True,
    description="The tools most likely needed for a piece of work, from what similar PAST loop steps actually "
                "used (four-minds FM-3): the similarity-weighted vote of the 15 nearest indexed steps, filled with "
                "the usage prior. Measured on 342 census steps: hit@5 0.91 vs 0.08 for caps.search. Input: text "
                "(str!), k (int, default 10), exclude_run (str: skip one run's own steps). Output: {tools: [{name, "
                "score, by}], index_rows}.",
)
async def cap_caps_shortlist(text: str = "", k: int = 10, exclude_run: str = "", trace_id=None) -> Dict[str, Any]:
    if not str(text or "").strip():
        return {"error": "text is required"}
    rows = await _rows()
    k = max(1, min(30, int(k or 10)))
    if not rows:
        return {"tools": [], "index_rows": 0, "degraded": True}
    # FM-9.1: the nearest past steps are found on an NLP node (one round trip: embed + search there)
    res = await DD.knn("tool_steps", _INDEX["version"], _push_rows, text=str(text)[:1000],
                       k=TC.NEIGHBOURS, exclude_run=exclude_run)
    if res.get("ok"):
        near = [(n["score"], n.get("payload") or []) for n in res.get("neighbours") or []]
        return {"tools": TC.rank_from_neighbours(near, _INDEX["prior"] or TC.prior(rows), k),
                "index_rows": len(rows), "degraded": False, "node": res.get("node"), "ms": res.get("ms")}
    if not await DD.local_allowed():
        return {"tools": [], "index_rows": len(rows), "degraded": True, "error": res.get("error")}
    vec = (await _embed([str(text)[:1000]]) or [None])[0]      # nlp.local: the host, off the event loop
    tools = await asyncio.to_thread(TC.shortlist, vec or [], rows, k, TC.NEIGHBOURS, exclude_run)
    return {"tools": tools, "index_rows": len(rows), "degraded": vec is None, "node": "host"}


@capability(
    "caps.shortlist.index", http_method="POST", http_path="/caps/shortlist/index", http_tags=["minds", "caps"],
    memory="off",
    description="Index the steps of recent loop runs for caps.shortlist (title, MiniLM vector, tools used, from "
                "run traces; embeddings only, no LLM). Runs nightly by itself; use this for a first fill. Input: "
                "limit_runs (int, default 400). Output: {ok, runs_seen, traced, steps_added}.",
)
async def cap_caps_shortlist_index(limit_runs: int = 400, trace_id=None) -> Dict[str, Any]:
    return await build_index(max(1, min(2000, int(limit_runs or 400))))


@capability(
    "caps.shortlist.report", http_method="GET", http_path="/caps/shortlist/report", http_tags=["minds", "caps"],
    memory="off", silent=True,
    description="How the caps.shortlist shadow did on live loop steps (four-minds FM-3): hit@5, recall@10 and MRR "
                "of the tools each step really used, from the loop.caps_shortlist decisions and their outcomes. "
                "Input: days (int, default 14). Output: {summary, index_rows}.",
)
async def cap_caps_shortlist_report(days: int = 14, trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"error": "postgres unavailable"}
    async with pool.acquire() as conn:
        recs = await conn.fetch(
            """SELECT l.decision_id,
                      COALESCE(array_agg((o.row->'detail'->>'rank')::int) FILTER (WHERE o.label = '"used"'), '{}') AS ranks,
                      count(o.decision_id) AS used_n
               FROM decision_ledger l LEFT JOIN decision_outcomes o ON o.decision_id = l.decision_id AND o.source = 'step.tools'
               WHERE l.question_id = $1 AND l.ts >= NOW() - make_interval(days => $2)
               GROUP BY l.decision_id""", TC.QUESTION, max(1, min(365, int(days or 14))))
    rows = [([int(x) for x in (r["ranks"] or []) if x is not None], int(r["used_n"] or 0)) for r in recs]
    return {"summary": TC.summarize([x for x in rows if x[1] > 0]), "index_rows": len(await _rows())}


def on_step(run_id: str, step_id: Any, title: str, used: List[str], origin: str = "user") -> None:
    """A finished loop step: record what the shortlist would have said, and what the step really used.
    Background, never raises, never waits."""
    try:
        if not run_id or not str(title or "").strip() or not used:
            return
        task = asyncio.get_running_loop().create_task(_record(run_id, step_id, str(title), list(used), origin))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("tool shortlist hook: %s", e)


async def _record(run_id: str, step_id: Any, title: str, used: List[str], origin: str) -> None:
    try:
        res = await cap_caps_shortlist(text=title, k=TC.K, exclude_run=run_id)   # keyword only: a @capability wrapper
        names = [t["name"] for t in res.get("tools") or []]
        if not names or res.get("degraded"):
            return
        d = TC.decision(names, run_id=run_id, step_id=step_id, origin=origin)
        try:
            from Vera.vera.minds import ledger_capabilities as L
        except Exception:                # pragma: no cover
            from vera.minds import ledger_capabilities as L  # type: ignore
        for row in [d] + TC.outcomes(d, used):
            await L.append(row)
    except Exception as e:
        log.debug("tool shortlist record: %s", e)


async def _night_job() -> None:
    """Rebuild the step index once a night. Prod only (a node worker keeps local time and has no business indexing);
    the night key is set only after the build succeeded, so a failure retries on the next tick."""
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
        log.info("caps.shortlist nightly index %s: %s", night, out)
    except Exception as e:
        log.warning("tool shortlist night job: %s", e)


schedule(_night_job, interval=900, name="caps_shortlist_index_night", singleton=True, skip_in_sandbox=True)
