"""decide.tests.status and the Loop Lab Tests page (/decide/tests): four-minds' tests in one view (see tests_core).

The schedule (entries, next windows, run history) says whether each test ran and how it ended; each test's own report
says what it found - read with a timeout and cached for five minutes, since some (the candidate matrix) are heavy.
Read-only: the page has no controls (a test is run, stopped or changed from the Schedule page).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, CAPABILITY_REGISTRY, capability

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import series_core as _SER
except Exception:                        # worktree / app-free import
    from vera.minds import series_core as _SER   # type: ignore

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import tests_core as TC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import tests_core as TC          # type: ignore
    from vera.minds import contract_core as _MC      # type: ignore

log = logging.getLogger("vera.minds.tests_monitor")
_HERE = Path(__file__).resolve().parent
HEADLINE_TTL_S = 300
CALL_TIMEOUT_S = 20.0
_CACHE: Dict[str, Any] = {"at": 0.0, "headlines": {}}


async def _call(name: str, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not loaded"}
    try:
        out = await asyncio.wait_for(fn(**kw), CALL_TIMEOUT_S)
        return out if isinstance(out, dict) else {"value": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _h_matrix() -> Dict[str, Any]:
    d = await _call("decide.matrix.report", days=7)
    if d.get("error"):
        return d
    best = {}
    for q, a in (d.get("applications") or {}).items():
        c = next((x for x in a.get("candidates") or [] if (x.get("outcomes_n") or 0) >= 30), None)
        if c:
            best[q] = f"{c['candidate']} {c.get('accuracy')} (live {((a.get('live') or {}).get('accuracy'))})"
    return {"pairs_scored": d.get("pairs_scored"), "pairs_untested": d.get("pairs_untested"),
            "applications": len(d.get("applications") or {}), "best_with_outcomes": best}


async def _h_shortlist() -> Dict[str, Any]:
    d = await _call("caps.shortlist.replay", history=1)
    snap = d.get("latest") or {}
    loop, chat = snap.get("loop") or {}, snap.get("chat") or {}
    return {"night": snap.get("night"), "index_rows": snap.get("index_rows"), "loop_n": loop.get("n"),
            "loop_hit@5": loop.get("hit@5"), "loop_recall@10": loop.get("recall@10"), "loop_mrr": loop.get("mrr"),
            "chat_turns_with_use": chat.get("turns_with_use"), "chat_unoffered_rate": chat.get("unoffered_rate"),
            "error": d.get("error")}


async def _h_memory() -> Dict[str, Any]:
    d = await _call("memory.perf.rows", kind="probe", days=14, limit=1)
    rows = d.get("rows") or []
    if not rows:
        return {"probe_rows": 0, "error": d.get("error")}
    r = rows[0]
    seek = (r.get("paths") or {}).get("memory.seek") or {}
    return {"at": r.get("ts"), "completed_at": r.get("ts"), "sha": r.get("sha_short"), "seek_p50_ms": (seek.get("total") or {}).get("p50"),
            "seek_p95_ms": (seek.get("total") or {}).get("p95"),
            "quality": (r.get("extra") or {}).get("quality")}


async def _h_chat_corpus() -> Dict[str, Any]:
    d = await _call("chat.corpus.last")
    # objects, not repr strings (vera-44, 18 Oct); broken = every case errored - a broken measure, never a 0% score
    if not isinstance(d, dict) or d.get("error") and not d.get("summary"):
        return {"error": (d or {}).get("error") if isinstance(d, dict) else "no answer"}
    summ = d.get("summary") or {}
    status = summ.get("status") or {}
    cases = summ.get("cases") or 0
    return {"run": d.get("run") or {}, "running": d.get("running"),
            "completed_at": (d.get("run") or {}).get("finished") or None,
            "cases": cases, "status": status, "pass_rate": summ.get("pass_rate"), "ttft_ms": summ.get("ttft_ms"),
            "failed_n": len(summ.get("failed") or []), "errors_n": len(summ.get("errors") or []),
            "broken": bool(cases) and status.get("error", 0) == cases}


async def _h_claims() -> Dict[str, Any]:
    d = await _call("decide.ledger.stats")
    q = next((x for x in d.get("questions") or [] if x.get("question_id") == "output.claim_supported"), {})
    return {"decisions": q.get("decisions", 0), "with_outcome": q.get("with_outcome", 0), "error": d.get("error")}


async def _h_calibration() -> Dict[str, Any]:
    d = await _call("decide.calibration.history")
    h = (d.get("weeks") or [None])[0] or {}
    return {"week": h.get("week"), "alerts": len(h.get("alerts") or []), "error": d.get("error")}


async def _h_cascade() -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    try:
        raw = await r.get("vera:decide:cascade:plan") if r is not None else None
        plan = json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"eligible": {q: f"{p.get('impl')} >= {p.get('tau')}" for q, p in plan.items() if p.get("eligible")},
            "questions": len(plan)}


HEADLINES: Dict[str, Callable[[], Awaitable[Dict[str, Any]]]] = {
    "matrix": _h_matrix, "shortlist": _h_shortlist, "memory": _h_memory, "chat_corpus": _h_chat_corpus,
    "claims": _h_claims, "calibration": _h_calibration, "cascade": _h_cascade}


async def headlines(force: bool = False) -> Dict[str, Any]:
    if not force and time.time() - _CACHE["at"] < HEADLINE_TTL_S and _CACHE["headlines"]:
        return _CACHE["headlines"]
    keys = list(HEADLINES)
    vals = await asyncio.gather(*(HEADLINES[k]() for k in keys), return_exceptions=True)
    out = {k: (v if isinstance(v, dict) else {"error": str(v)[:120]}) for k, v in zip(keys, vals)}
    _CACHE.update(at=time.time(), headlines=out)
    return out


@capability(
    "decide.tests.status", http_method="GET", http_path="/decide/tests/status", http_tags=["minds", "evolve"],
    contract=_MC.contract("decide.tests.status", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Four-minds' tests as the Loop Lab Tests page shows them: per test (candidate matrix, claims harvest, "
                "tool-choice replay, precedent index, cascade plan, calibration, memory census, chat corpus, census "
                "arms) its schedule entries with next fire, last result, recover / protect, its health (failed, "
                "interrupted, missed, recovering, running, never, ok, disabled), its last runs, and its report's "
                "headline (cached 5 min). Input: refresh (bool - re-read the headlines now). Output: {tests, counts, "
                "at, headlines_at}.",
)
async def cap_decide_tests_status(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    lst = await _call("evolve.schedule.list")
    hist = await _call("evolve.schedule.history", limit=300)
    heads = await headlines(force=bool(refresh))
    out = TC.status(lst.get("schedules") or [], hist.get("runs") or [], lst.get("next") or {},
                    datetime.now(timezone.utc), heads, background=await _background())
    out["headlines_at"] = datetime.fromtimestamp(_CACHE["at"], timezone.utc).isoformat(timespec="seconds") \
        if _CACHE["at"] else None
    if lst.get("error") or hist.get("error"):
        out["error"] = lst.get("error") or hist.get("error")
    return out


async def _background() -> Dict[str, Any]:
    """The background queue the matrix's judges wait in (vera-d2, 19 Oct): depth, oldest wait, four-minds jobs."""
    d = await _call("background.status")
    q = (d or {}).get("queue") or {}
    w = q.get("waiting") or []
    mine = [x for x in w if (x.get("kind") or "").split(":")[0] in ("cap",) and any(
        k in str(x.get("title") or "") for k in ("shadow judge", "call-site", "decide", "matrix"))]
    kinds: Dict[str, int] = {}
    for x in w:
        kk = (x.get("kind") or "").split(":")[0]
        kinds[kk] = kinds.get(kk, 0) + 1
    return {"depth": q.get("depth"), "oldest_wait_s": max((x.get("waiting_for_s") or 0) for x in w) if w else 0,
            "four_minds_waiting": len(mine),
            "four_minds_oldest_wait_s": max((x.get("waiting_for_s") or 0) for x in mine) if mine else 0,
            "by_kind": kinds, "running": q.get("running"), "error": (d or {}).get("error")}


# The stop-gap page (/decide/tests, tests_panel.html) retired 18 Oct: Loop Lab's own Tests section (looplab-control
# S2) draws the tests from decide.tests.status and the report caps.



# -- dated history for the Loop Lab Tests timeline (vera-44, 18 Oct; LOOPLAB-TESTS-HANDOVER §5 lanes 4, 7, 8) -------
_SERIES_DDL = [
    """CREATE TABLE IF NOT EXISTS four_minds_snapshots (
           id    BIGSERIAL PRIMARY KEY,
           ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           kind  TEXT NOT NULL,
           data  JSONB NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS four_minds_snapshots_kind_ts ON four_minds_snapshots (kind, ts)",
]


@capability(
    "decide.series.snapshot", http_method="POST", http_path="/decide/series/snapshot", http_tags=["minds"],
    memory="off",
    description="Store one row of each four-minds history series (Loop Lab Tests timeline): matrix coverage "
                "(decide.matrix.report), claims accuracy per judge vs the live rule, worldview spread "
                "(worldview.health). Runs on the Loop Lab schedule. Output: {ok, stored: [kind]}.",
)
async def cap_decide_series_snapshot(trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"ok": False, "error": "postgres unavailable"}
    report = await _call("decide.matrix.report", days=7)
    health = await _call("worldview.health")
    tests = await cap_decide_tests_status(refresh=True)
    rows = [(_SER.KINDS["matrix"], _SER.matrix_row(report)), (_SER.KINDS["claims"], _SER.claims_row(report)),
            (_SER.KINDS["worldview"], _SER.worldview_row(health)),
            ("series.tests", {t["id"]: {"health": t.get("health"), "headline": t.get("headline")}
                              for t in (tests or {}).get("tests") or []} or None)]
    stored = []
    async with pool.acquire() as conn:
        for st in _SERIES_DDL:
            await conn.execute(st)
        for kind, data in rows:
            if data:
                await conn.execute("INSERT INTO four_minds_snapshots (kind, data) VALUES ($1, $2::jsonb)",
                                   kind, json.dumps(data, default=str))
                stored.append(kind)
    return {"ok": True, "stored": stored}


async def _series(kind: str, days: int) -> Dict[str, Any]:
    pool = getattr(_orch, "PG_POOL", None)
    if pool is None:
        return {"rows": [], "error": "postgres unavailable"}
    try:
        days = max(1, min(365, int(days or 30)))
    except Exception:
        days = 30
    try:
        async with pool.acquire() as conn:
            recs = await conn.fetch(
                """SELECT ts, data FROM four_minds_snapshots WHERE kind = $1
                   AND ts >= NOW() - make_interval(days => $2) ORDER BY ts DESC LIMIT 2000""", kind, days)
    except Exception as e:                       # the table appears with the first snapshot
        return {"rows": [], "error": str(e)[:200]}
    out = []
    for r in recs:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
        out.append(dict(d, ts=r["ts"].isoformat()))
    return {"rows": out, "kind": kind, "days": days}


@capability(
    "decide.matrix.history", http_method="GET", http_path="/decide/matrix/history", http_tags=["minds"],
    memory="off", silent=True,
    description="Candidate-matrix coverage over time (Loop Lab Tests lane): per snapshot pairs_scored, "
                "pairs_untested, applications, candidates, pairs_beating_live and the LLM rotation. "
                "Input: days (int, default 30). Output: {rows: [{ts, ...}]} newest first.",
)
async def cap_decide_matrix_history(days: int = 30, trace_id=None) -> Dict[str, Any]:
    return await _series(_SER.KINDS["matrix"], days)


@capability(
    "decide.claims.history", http_method="GET", http_path="/decide/claims/history", http_tags=["minds"],
    memory="off", silent=True,
    description="Claims-check accuracy over time (FM-5.2, Loop Lab Tests lane): per snapshot the live rule's accuracy, "
                "every judge's accuracy and outcomes_n, the best judge with >= 30 outcomes and whether it beats the "
                "rule. Input: days (int, default 30). Output: {rows: [{ts, ...}]} newest first.",
)
async def cap_decide_claims_history(days: int = 30, trace_id=None) -> Dict[str, Any]:
    return await _series(_SER.KINDS["claims"], days)


@capability(
    "worldview.spread.history", http_method="GET", http_path="/worldview/spread/history", http_tags=["worldview"],
    memory="off", silent=True,
    description="The worldview's spread over time (FM-W.1, Loop Lab Tests lane): per snapshot the median concept "
                "population, the share and count of concepts below 20 records, the largest concept's share, records "
                "per concept and the health status. Input: days (int, default 30). Output: {rows: [{ts, ...}]} newest first.",
)
async def cap_worldview_spread_history(days: int = 30, trace_id=None) -> Dict[str, Any]:
    return await _series(_SER.KINDS["worldview"], days)



@capability(
    "decide.tests.history", http_method="GET", http_path="/decide/tests/history", http_tags=["minds"],
    memory="off", silent=True,
    description="Every four-minds test's health and report headline over time (vera-d2, 19 Oct: trends without "
                "scraping), one row per decide.series.snapshot: {rows: [{ts, <test id>: {health, headline}}]} newest "
                "first. Input: days (int, default 30).",
)
async def cap_decide_tests_history(days: int = 30, trace_id=None) -> Dict[str, Any]:
    return await _series("series.tests", days)
