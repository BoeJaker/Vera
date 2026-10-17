"""canvas.engagement.*: what a person did with the canvas items (four-minds FM-6, Q7; see engagement_core).

The canvas element batches its events and posts them to canvas.engagement.record; rows go to Postgres
(canvas_engagement) and a daily job keeps 30 days. canvas.engagement.report reads them per surface - the
outcomes the curator (canvas.surface_rank) needs before it may ever leave shadow.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.canvas import engagement_core as EC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.canvas import engagement_core as EC          # type: ignore
    from vera.minds import contract_core as _MC            # type: ignore

log = logging.getLogger("vera.canvas.engagement")
_DDL = (
    """CREATE TABLE IF NOT EXISTS canvas_engagement (
           id          BIGSERIAL PRIMARY KEY,
           ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           event       TEXT NOT NULL,
           surface     TEXT NOT NULL,
           kind        TEXT NOT NULL DEFAULT '',
           act         TEXT NOT NULL DEFAULT '',
           item        TEXT NOT NULL DEFAULT '',
           canvas_id   TEXT NOT NULL DEFAULT '',
           session_id  TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS canvas_engagement_ts ON canvas_engagement (ts)",
    "CREATE INDEX IF NOT EXISTS canvas_engagement_surface ON canvas_engagement (surface, ts)",
)
_DDL_DONE = False
# a page that misbehaves cannot flood the table: at most this many events a minute are taken in
MAX_PER_MINUTE = 600
_RATE: Dict[str, Any] = {"minute": 0, "n": 0}
_STATS: Dict[str, int] = {"recorded": 0, "rejected": 0, "rate_limited": 0, "dropped_no_db": 0}


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in _DDL:
            await conn.execute(s)
        _DDL_DONE = True


def _admit(n: int, now: Optional[float] = None) -> int:
    minute = int((now if now is not None else time.time()) // 60)
    if _RATE["minute"] != minute:
        _RATE.update(minute=minute, n=0)
    take = max(0, min(n, MAX_PER_MINUTE - _RATE["n"]))
    _RATE["n"] += take
    return take


@capability(
    "canvas.engagement.record", http_method="POST", http_path="/canvas/engagement/record",
    contract=_MC.contract("canvas.engagement.record", effects=["write"], idempotency="at_least_once"),
    http_tags=["canvas"], memory="off", silent=True,
    description="Record what a person did with canvas items (four-minds FM-6, Q7), as the canvas element reports "
                "it: events [{event: opened|used|closed|restored, surface, kind, act, key, canvas, session}] (at "
                "most 50 a call, 600 a minute). Kept 30 days; read by canvas.engagement.report. Output: {ok, "
                "recorded, rejected}.",
)
async def cap_canvas_engagement_record(events: Optional[List[Dict[str, Any]]] = None, session_id: str = "",
                                       trace_id=None) -> Dict[str, Any]:
    raw = events if isinstance(events, list) else []
    rows = EC.clean(raw, session_id)
    rejected = len(raw) - len(rows)
    take = _admit(len(rows))
    _STATS["rejected"] += rejected
    _STATS["rate_limited"] += len(rows) - take
    rows = rows[:take]
    if not rows:
        return {"ok": True, "recorded": 0, "rejected": rejected}
    pool = _orch.PG_POOL
    if pool is None:
        _STATS["dropped_no_db"] += len(rows)
        return {"ok": False, "error": "postgres unavailable", "recorded": 0}
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.executemany(
            "INSERT INTO canvas_engagement (event, surface, kind, act, item, canvas_id, session_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            [(r["event"], r["surface"], r["kind"], r["act"], r["item"], r["canvas_id"], r["session_id"]) for r in rows])
    _STATS["recorded"] += len(rows)
    return {"ok": True, "recorded": len(rows), "rejected": rejected}


@capability(
    "canvas.engagement.report", http_method="GET", http_path="/canvas/engagement/report",
    contract=_MC.contract("canvas.engagement.report", effects=["read"], idempotency="idempotent"),
    http_tags=["canvas"], memory="off", silent=True,
    description="What people did with each canvas surface over the last `days` (four-minds FM-6, Q7): counts of "
                "opened / used / closed / restored and engagement = (opened + used + restored) / all, most-handled "
                "first - the outcomes the canvas curator (canvas.surface_rank) is judged by. Inputs: days (int=14, "
                "max 30), min_events (int=1). Output: {surfaces, totals, events, days, process}.",
)
async def cap_canvas_engagement_report(days: int = 14, min_events: int = 1, trace_id=None) -> Dict[str, Any]:
    pool = _orch.PG_POOL
    if pool is None:
        return {"surfaces": [], "error": "postgres unavailable"}
    d = max(1, min(EC.RETAIN_DAYS, int(days or 14)))
    since = datetime.now(timezone.utc) - timedelta(days=d)
    async with pool.acquire() as conn:
        await _ensure(conn)
        recs = await conn.fetch("SELECT surface, event, count(*) AS n FROM canvas_engagement WHERE ts >= $1 "
                                "GROUP BY surface, event", since)
    out = EC.report([dict(r) for r in recs], max(1, int(min_events or 1)))
    return dict(out, days=d, process=dict(_STATS))


async def _retention_job() -> None:
    pool = _orch.PG_POOL
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await _ensure(conn)
            await conn.execute("DELETE FROM canvas_engagement WHERE ts < NOW() - make_interval(days => $1)",
                               EC.RETAIN_DAYS)
    except Exception as e:
        log.warning("canvas engagement retention: %s", e)


schedule(_retention_job, interval=86400, name="canvas_engagement_retention", singleton=True, skip_in_sandbox=True)
