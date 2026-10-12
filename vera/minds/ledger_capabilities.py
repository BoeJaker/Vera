"""The decision ledger (four-minds FM-0.2, ROADMAP §5.1): write path, drainer, capabilities.

Write path: a Decision v0 or Outcome v0 row is checked (ledger_core) and appended to the
Redis stream `vera:decide:ledger` with one XADD (O(1), no Postgres await), so recording a
decision on a hot path costs one Redis round trip.

Drainer: a singleton scheduled job reads the stream through a consumer group and writes
the rows to Postgres - decisions into `decision_ledger`, partitioned by month, outcomes into
`decision_outcomes` - then acknowledges them. A row is acknowledged only after Postgres has
it, so rows survive a restart or a Postgres outage. Tables and partitions are created
idempotently, as vera_memories is (Vera has no migrations directory).

Capabilities: decide.record, decide.ledger, decide.ledger.import, decide.ledger.stats.
Not in this slice: decide.ask, the run-end outcome hook, the orphan sweeper, calibration.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import ledger_core as LC
except Exception:                        # worktree / app-free import
    from vera.minds import ledger_core as LC   # type: ignore

log = logging.getLogger("vera.minds.ledger")

STREAM = "vera:decide:ledger"
OPEN_RUNS = "vera:decide:open_runs"        # run_id -> epoch s of its first decision
ORPHAN_AFTER_S = float(__import__("os").environ.get("VERA_DECIDE_ORPHAN_AFTER_S", "21600") or 21600)
GROUP = "drain"
CONSUMER = "orchestrator"
STREAM_MAXLEN = 200_000
DRAIN_BATCH = 500

_STATS: Dict[str, int] = {"appended": 0, "refused": 0, "dropped_no_redis": 0,
                          "drained_decisions": 0, "drained_outcomes": 0, "drain_errors": 0,
                          "orphaned_runs": 0, "interrupted_outcomes": 0}
_DDL_DONE = False
_PARTITIONS: set = set()

DDL = [
    """CREATE TABLE IF NOT EXISTS decision_ledger (
           decision_id TEXT NOT NULL,
           ts          TIMESTAMPTZ NOT NULL,
           origin      TEXT, surface TEXT, question_id TEXT, decider TEXT, mode TEXT,
           answer      JSONB, p DOUBLE PRECISION,
           run_id      TEXT, session_id TEXT,
           row         JSONB NOT NULL,
           PRIMARY KEY (decision_id, ts)
       ) PARTITION BY RANGE (ts)""",
    "CREATE INDEX IF NOT EXISTS decision_ledger_question_ts ON decision_ledger (question_id, ts)",
    """CREATE TABLE IF NOT EXISTS decision_outcomes (
           id          BIGSERIAL PRIMARY KEY,
           decision_id TEXT NOT NULL,
           ts          TIMESTAMPTZ NOT NULL,
           label       JSONB, source TEXT, strength TEXT,
           row         JSONB NOT NULL,
           UNIQUE (decision_id, source, ts)
       )""",
    "CREATE INDEX IF NOT EXISTS decision_outcomes_decision ON decision_outcomes (decision_id)",
]


def _kind(row: Dict[str, Any]) -> str:
    return "outcome" if (row or {}).get("schema") == LC.OUTCOME_SCHEMA else "decision"


def _problems(row: Dict[str, Any]) -> List[str]:
    return LC.check_outcome(row) if _kind(row) == "outcome" else LC.check_decision(row)


async def append(row: Dict[str, Any]) -> Dict[str, Any]:
    """Check one row and append it to the ledger stream. The hot-path entry point."""
    errs = _problems(row)
    if errs:
        _STATS["refused"] += 1
        return {"ok": False, "errors": errs}
    r = _orch.REDIS
    if r is None:
        _STATS["dropped_no_redis"] += 1
        return {"ok": False, "errors": ["redis unavailable"]}
    await r.xadd(STREAM, {"kind": _kind(row), "row": json.dumps(row, default=str)},
                 maxlen=STREAM_MAXLEN, approximate=True)
    _STATS["appended"] += 1
    return {"ok": True, "decision_id": row["decision_id"], "kind": _kind(row)}


def _dt(ts: str) -> datetime:
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _s(v: Any) -> str:
    return v if isinstance(v, str) else v.decode("utf-8")


async def _ensure_tables(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for stmt in DDL:
            await conn.execute(stmt)
        _DDL_DONE = True


async def _ensure_partition(conn, ts: str) -> None:
    part = LC.partition_for(ts)
    if part["name"] in _PARTITIONS:
        return
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {part['name']} PARTITION OF decision_ledger "
        f"FOR VALUES FROM ('{part['from']}') TO ('{part['to']}')")
    _PARTITIONS.add(part["name"])


async def _write(conn, kind: str, row: Dict[str, Any]) -> None:
    if kind == "outcome":
        await conn.execute(
            """INSERT INTO decision_outcomes (decision_id, ts, label, source, strength, row)
               VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb) ON CONFLICT DO NOTHING""",
            row["decision_id"], _dt(row["ts"]), json.dumps(row.get("label")), row.get("source"),
            row.get("strength"), json.dumps(row, default=str))
        _STATS["drained_outcomes"] += 1
        return
    await _ensure_partition(conn, row["ts"])
    await conn.execute(
        """INSERT INTO decision_ledger (decision_id, ts, origin, surface, question_id, decider, mode,
                                        answer, p, run_id, session_id, row)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12::jsonb)
           ON CONFLICT DO NOTHING""",
        row["decision_id"], _dt(row["ts"]), row.get("origin"), row.get("surface"),
        row.get("question_id"), row.get("decider"), row.get("mode"), json.dumps(row.get("answer")),
        row.get("p"), row.get("run_id"), row.get("session_id"), json.dumps(row, default=str))
    _STATS["drained_decisions"] += 1


async def drain_once(batch: int = DRAIN_BATCH) -> Dict[str, Any]:
    """Move up to `batch` rows from the stream into Postgres. Own pending rows first."""
    r, pool = _orch.REDIS, _orch.PG_POOL
    if r is None or pool is None:
        return {"ok": False, "skipped": "redis or postgres unavailable"}
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:                       # BUSYGROUP: it exists
        if "BUSYGROUP" not in str(e):
            raise
    moved = 0
    for start in ("0", ">"):                     # redeliver what an earlier drain didn't ack
        got = await r.xreadgroup(GROUP, CONSUMER, {STREAM: start}, count=batch)
        entries = got[0][1] if got else []
        if not entries:
            continue
        acked = []
        try:
            async with pool.acquire() as conn:
                await _ensure_tables(conn)
                for entry_id, fields in entries:
                    # A malformed row can never be stored: acknowledge and skip it. A database
                    # error is different - stop, so it and the rest stay pending for next time.
                    try:
                        fields = {_s(k): _s(v) for k, v in (fields or {}).items()}
                        row = json.loads(fields.get("row") or "{}")
                        kind = fields.get("kind") or _kind(row)
                        bad = _problems(row)
                    except Exception as e:
                        bad = [str(e)]
                    if bad:
                        _STATS["drain_errors"] += 1
                        log.warning("decision ledger: row %s unreadable (%s) - skipped",
                                    _s(entry_id), "; ".join(bad)[:200])
                        acked.append(entry_id)
                        continue
                    await _write(conn, kind, row)
                    acked.append(entry_id)
        finally:
            if acked:
                await r.xack(STREAM, GROUP, *acked)
                moved += len(acked)
    return {"ok": True, "moved": moved}


async def _drain_job() -> None:
    try:
        await drain_once()
    except Exception as e:
        _STATS["drain_errors"] += 1
        log.warning("decision ledger drain: %s", e)


schedule(_drain_job, interval=10, name="decide_ledger_drain", singleton=True, skip_in_sandbox=True)


async def mark_run(run_id: str, open_: bool) -> None:
    """A run with decisions is open until its end hook closes it (the sweeper's input)."""
    r = _orch.REDIS
    if r is None or not run_id:
        return
    import time as _t
    if open_:
        await r.hsetnx(OPEN_RUNS, run_id, str(int(_t.time())))
    else:
        await r.hdel(OPEN_RUNS, run_id)


async def sweep_orphans(now: Optional[float] = None, older_than_s: float = ORPHAN_AFTER_S) -> Dict[str, Any]:
    """Runs still open after `older_than_s` never reached their end (a restart, a release, a
    cancel). Each of their decisions without an outcome gets one: label "interrupted",
    source sweeper.interrupted, strength weak - so calibration can count them instead of
    silently dropping the long and failing runs that get killed most."""
    import time as _t
    r, pool = _orch.REDIS, _orch.PG_POOL
    if r is None or pool is None:
        return {"ok": False, "skipped": "redis or postgres unavailable"}
    now = float(now if now is not None else _t.time())
    opened = {(_s(k)): float(_s(v) or 0) for k, v in ((await r.hgetall(OPEN_RUNS)) or {}).items()}
    stale = [rid for rid, ts in opened.items() if now - ts >= older_than_s]
    marked = 0
    for rid in stale:
        async with pool.acquire() as conn:
            await _ensure_tables(conn)
            rows = await conn.fetch(
                """SELECT l.decision_id FROM decision_ledger l
                   WHERE l.run_id = $1 AND NOT EXISTS
                     (SELECT 1 FROM decision_outcomes o WHERE o.decision_id = l.decision_id)""", rid)
        for row in rows:
            res = await append(LC.make_outcome(row["decision_id"], "interrupted", "sweeper.interrupted",
                                               "weak", detail={"open_s": int(now - opened[rid])}))
            marked += 1 if res.get("ok") else 0
        await r.hdel(OPEN_RUNS, rid)
        _STATS["orphaned_runs"] += 1
    _STATS["interrupted_outcomes"] += marked
    return {"ok": True, "open": len(opened), "orphaned": len(stale), "interrupted_outcomes": marked}


async def _sweep_job() -> None:
    try:
        await sweep_orphans()
    except Exception as e:
        log.warning("decision ledger sweep: %s", e)


schedule(_sweep_job, interval=900, name="decide_ledger_sweep", singleton=True, skip_in_sandbox=True)


def _rows_arg(rows: Any) -> List[Dict[str, Any]]:
    if isinstance(rows, list):
        return rows
    if isinstance(rows, dict):
        return [rows]
    text = str(rows or "").strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "decide.record", http_method="POST", http_path="/decide/record", http_tags=["minds"],
    contract=_MC.contract("decision.record", effects=["write"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Record one Decision v0 or Outcome v0 row in the decision ledger (four-minds FM-0.2). "
                "The row is checked against the frozen v0 shape (ROADMAP §5.1) and appended to a Redis "
                "stream; a background job moves it to Postgres. Input: row (object or JSON string), inputs "
                "(optional object: what the decision saw, kept in the training store when its surface is "
                "enabled, so it can be rated in /decide/labels). Output: {ok, decision_id, kind} or "
                "{ok:false, errors[]}.",
)
async def cap_decide_record(row: Any = None, inputs: Any = None, trace_id=None) -> Dict[str, Any]:
    rows = _rows_arg(row)
    if len(rows) != 1:
        return {"ok": False, "errors": ["pass exactly one row"]}
    out = await append(rows[0])
    if out.get("ok") and isinstance(inputs, dict) and inputs and out.get("kind") == "decision":
        ts = _training_state()
        if ts is not None:
            ts.put(rows[0], inputs)
    return out


def _training_state():
    """The LIVE training_state module (the loader loads it under its bare name, after this one)."""
    return sys.modules.get("training_state") or sys.modules.get("Vera.vera.minds.training_state")


@capability(
    "decide.ledger.import", http_method="POST", http_path="/decide/ledger/import", http_tags=["minds"],
    contract=_MC.contract("decision.ledger.import", effects=["write"], idempotency="idempotent"),
    memory="off",
    description="Import Decision v0 / Outcome v0 rows kept by an early emitter (intel's own store) into "
                "the decision ledger, unchanged. Input: rows (list, JSON array, or JSONL), dry_run "
                "(bool, default true: check only). Output: {ok, checked, valid, appended, refused, "
                "errors (first 20)}. Re-importing is safe: Postgres keeps one row per id.",
)
async def cap_decide_ledger_import(rows: Any = None, dry_run: bool = True, trace_id=None) -> Dict[str, Any]:
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("0", "false", "no")
    try:
        items = _rows_arg(rows)
    except Exception as e:
        return {"ok": False, "errors": [f"rows unreadable: {e}"]}
    errors, valid, appended = [], 0, 0
    for i, row in enumerate(items):
        errs = _problems(row)
        if errs:
            errors.append({"index": i, "decision_id": (row or {}).get("decision_id"), "errors": errs})
            continue
        valid += 1
        if not dry_run:
            res = await append(row)
            if res.get("ok"):
                appended += 1
            else:
                errors.append({"index": i, "decision_id": row.get("decision_id"), "errors": res.get("errors")})
    return {"ok": not errors, "dry_run": bool(dry_run), "checked": len(items), "valid": valid,
            "appended": appended, "refused": len(errors), "errors": errors[:20]}


@capability(
    "decide.ledger", http_method="GET", http_path="/decide/ledger", http_tags=["minds"],
    contract=_MC.contract("decision.ledger.read", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Read the decision ledger (four-minds FM-0.2): the latest decisions, newest first, each "
                "with its outcomes and the strongest one. Input: question_id, surface, run_id, "
                "session_id (str filters), since (ISO date), limit (int, default 50, max 500). "
                "Output: {decisions[], count}.",
)
async def cap_decide_ledger(question_id: str = "", surface: str = "", run_id: str = "",
                            session_id: str = "", since: str = "", limit: int = 50,
                            trace_id=None) -> Dict[str, Any]:
    pool = _orch.PG_POOL
    if pool is None:
        return {"decisions": [], "count": 0, "error": "postgres unavailable"}
    try:
        limit = max(1, min(500, int(limit)))
    except Exception:
        limit = 50
    where, params = [], []
    for col, val in (("question_id", question_id), ("surface", surface), ("run_id", run_id),
                     ("session_id", session_id)):
        if val:
            params.append(val)
            where.append(f"{col} = ${len(params)}")
    if since:
        try:
            params.append(_dt(since))
            where.append(f"ts >= ${len(params)}")
        except Exception:
            return {"decisions": [], "count": 0, "error": "since is not ISO-8601"}
    sql = ("SELECT row FROM decision_ledger" + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY ts DESC LIMIT {limit}")
    async with pool.acquire() as conn:
        await _ensure_tables(conn)
        rows = [json.loads(r["row"]) if isinstance(r["row"], str) else r["row"]
                for r in await conn.fetch(sql, *params)]
        outs: Dict[str, List[Dict[str, Any]]] = {}
        if rows:
            for o in await conn.fetch("SELECT row FROM decision_outcomes WHERE decision_id = ANY($1::text[])",
                                      [d["decision_id"] for d in rows]):
                orow = json.loads(o["row"]) if isinstance(o["row"], str) else o["row"]
                outs.setdefault(orow["decision_id"], []).append(orow)
    for d in rows:
        d["outcomes"] = outs.get(d["decision_id"], [])
        d["outcome"] = LC.strongest(d["outcomes"])
    return {"decisions": rows, "count": len(rows)}


@capability(
    "decide.ledger.stats", http_method="GET", http_path="/decide/ledger/stats", http_tags=["minds"],
    contract=_MC.contract("decision.ledger.stats", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Decision ledger health (four-minds FM-0.2): this process's append/refuse/drop/drain "
                "counters, the stream's length and undrained rows, and decisions per question with "
                "how many have an outcome. Output: {process, stream, questions[]}.",
)
async def cap_decide_ledger_stats(trace_id=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"process": dict(_STATS), "stream": {}, "questions": []}
    r = _orch.REDIS
    if r is not None:
        try:
            out["open_runs"] = await r.hlen(OPEN_RUNS)
        except Exception:
            pass
        try:
            out["stream"]["length"] = await r.xlen(STREAM)
            pend = await r.xpending(STREAM, GROUP)
            out["stream"]["pending"] = pend.get("pending") if isinstance(pend, dict) else pend
        except Exception as e:
            out["stream"]["error"] = str(e)[:200]
    pool = _orch.PG_POOL
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                await _ensure_tables(conn)
                for q in await conn.fetch(
                        """SELECT l.question_id, count(*) AS decisions,
                                  count(DISTINCT o.decision_id) AS with_outcome
                           FROM decision_ledger l
                           LEFT JOIN decision_outcomes o ON o.decision_id = l.decision_id
                           GROUP BY l.question_id ORDER BY decisions DESC LIMIT 100"""):
                    out["questions"].append(dict(q))
        except Exception as e:
            out["questions_error"] = str(e)[:200]
    return out
