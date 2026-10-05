"""Hourly snapshots for the four-minds baselines (FM-0.4).

retrieval.timing and chat.turns.stats are process windows (the last 500 calls) that a
restart empties, and prod restarts several times a day. The 7-day baseline FM-0.4 asks for
needs history, so once an hour this job stores:

- retrieval: memory.RETRIEVAL_TIMING.summary() - p50/p95 per path and stage, outcome counts;
- chat:      agents.CHAT_TURNS.summary()       - TTFT and step p50/p95 per agent|kind;
- decisions: the last hour's ledger rows by question_id and origin (with outcome counts);

in Postgres (four_minds_snapshots), and `baseline.snapshots` reads them back. Window
summaries overlap from hour to hour (each is the last 500 calls); the report reads them as
a time series, not as sums.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

log = logging.getLogger("vera.minds.baseline")

TABLE_DDL = [
    """CREATE TABLE IF NOT EXISTS four_minds_snapshots (
           id    BIGSERIAL PRIMARY KEY,
           ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           kind  TEXT NOT NULL,
           data  JSONB NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS four_minds_snapshots_kind_ts ON four_minds_snapshots (kind, ts)",
]
_DDL_DONE = False
_LAST: Dict[str, Any] = {}


def _module(*names: str):
    for n in names:
        m = sys.modules.get(n)
        if m is not None:
            return m
    return None


def collect_windows() -> Dict[str, Any]:
    """The process windows as they are now ({} for a window this process doesn't have)."""
    out: Dict[str, Any] = {}
    mem = _module("Vera.vera.fabric.memory", "memory")
    w = getattr(mem, "RETRIEVAL_TIMING", None)
    if w is not None:
        out["retrieval"] = w.summary()
    ag = _module("Vera.vera.agents.agents", "agents")
    w = getattr(ag, "CHAT_TURNS", None)
    if w is not None:
        out["chat"] = w.summary()
    return out


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in TABLE_DDL:
            await conn.execute(s)
        _DDL_DONE = True


async def snapshot_once() -> Dict[str, Any]:
    pool = _orch.PG_POOL
    if pool is None:
        return {"ok": False, "skipped": "postgres unavailable"}
    rows: List[tuple] = [(k, v) for k, v in collect_windows().items() if v]
    async with pool.acquire() as conn:
        await _ensure(conn)
        try:
            dec = await conn.fetch(
                """SELECT l.question_id, l.origin, count(*) AS decisions,
                          count(DISTINCT o.decision_id) AS with_outcome
                   FROM decision_ledger l
                   LEFT JOIN decision_outcomes o ON o.decision_id = l.decision_id
                   WHERE l.ts >= NOW() - INTERVAL '1 hour'
                   GROUP BY l.question_id, l.origin""")
            rows.append(("decisions", {"window": "1h", "rows": [dict(r) for r in dec]}))
        except Exception as e:                  # the ledger tables may not exist yet
            log.debug("baseline decisions: %s", e)
        for kind, data in rows:
            await conn.execute("INSERT INTO four_minds_snapshots (kind, data) VALUES ($1, $2::jsonb)",
                               kind, json.dumps(data, default=str))
    _LAST.update({"at": time.time(), "kinds": [k for k, _ in rows]})
    return {"ok": True, "stored": [k for k, _ in rows]}


async def _snapshot_job() -> None:
    try:
        await snapshot_once()
    except Exception as e:
        log.warning("baseline snapshot: %s", e)


schedule(_snapshot_job, interval=3600, name="four_minds_baseline_snapshot", singleton=True, skip_in_sandbox=True)


@capability(
    "baseline.snapshots", http_method="GET", http_path="/minds/baseline/snapshots",
    http_tags=["minds"], memory="off", silent=True,
    description="Four-minds baseline history (FM-0.4): hourly snapshots of retrieval.timing, "
                "chat.turns.stats and the last hour's ledger decisions by question and origin, kept "
                "across restarts. Input: kind (retrieval|chat|decisions, optional), days (int, "
                "default 7, max 31), limit (int, default 200, max 2000). Output: {snapshots: [{ts, "
                "kind, data}], count, last_run}.",
)
async def cap_baseline_snapshots(kind: str = "", days: int = 7, limit: int = 200, trace_id=None) -> Dict[str, Any]:
    pool = _orch.PG_POOL
    if pool is None:
        return {"snapshots": [], "count": 0, "error": "postgres unavailable"}
    try:
        days = max(1, min(31, int(days)))
        limit = max(1, min(2000, int(limit)))
    except Exception:
        days, limit = 7, 200
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.acquire() as conn:
        await _ensure(conn)
        if kind:
            rows = await conn.fetch("SELECT ts, kind, data FROM four_minds_snapshots WHERE kind = $1 AND ts >= $2 "
                                    "ORDER BY ts DESC LIMIT $3", kind, since, limit)
        else:
            rows = await conn.fetch("SELECT ts, kind, data FROM four_minds_snapshots WHERE ts >= $1 "
                                    "ORDER BY ts DESC LIMIT $2", since, limit)
    snaps = [{"ts": r["ts"].isoformat(), "kind": r["kind"],
              "data": json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]} for r in rows]
    return {"snapshots": snaps, "count": len(snaps), "last_run": dict(_LAST)}
