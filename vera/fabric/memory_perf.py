"""memory.perf.*: the memory performance log (four-minds FM-7.9; see memory_perf_core for why).

- every 15 minutes a live row per source (retrieval, chat) holding only the calls since the
  last row, tagged with the running sha and the memory switch modes, in Postgres (memory_perf);
- memory.perf.probe: the frozen known-item set through memory.seek, its stages captured from
  the retrieval window, stored as a probe row (run from a Loop Lab schedule entry at night);
- memory.perf.versions / rows / compare read them back: "what did this change do to memory".

The probe's own seek calls are kept out of the live rows: a live row is written just before
it and the live marks move past it after.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.fabric import memory_perf_core as MP
    from Vera.vera.minds import switchboard_core as SB
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.fabric import memory_perf_core as MP           # type: ignore
    from vera.minds import switchboard_core as SB            # type: ignore
    from vera.minds import contract_core as _MC              # type: ignore

log = logging.getLogger("vera.fabric.memory_perf")

INTERVAL_S = 900
KEEP_DAYS = 180
TABLE_DDL = [
    """CREATE TABLE IF NOT EXISTS memory_perf (
           id            BIGSERIAL PRIMARY KEY,
           ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           kind          TEXT NOT NULL,
           source        TEXT NOT NULL,
           sha_short     TEXT NOT NULL DEFAULT '',
           minds_digest  TEXT NOT NULL DEFAULT '',
           calls         INTEGER NOT NULL DEFAULT 0,
           data          JSONB NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS memory_perf_ts ON memory_perf (ts)",
    "CREATE INDEX IF NOT EXISTS memory_perf_sha ON memory_perf (sha_short, kind)",
]
_DDL_DONE = False
# source -> (window module names, window attribute)
SOURCES = {"retrieval": (("Vera.vera.fabric.memory", "memory"), "RETRIEVAL_TIMING"),
           "chat": (("Vera.vera.agents.agents", "agents"), "CHAT_TURNS")}
_STATE: Dict[str, Any] = {"marks": {}, "at": time.time(), "last": {}, "pruned": 0.0}
_LOCK = asyncio.Lock()


def _window(source: str):
    names, attr = SOURCES[source]
    for n in names:
        m = sys.modules.get(n)
        if m is not None and getattr(m, attr, None) is not None:
            return getattr(m, attr)
    return None


def _provenance() -> Dict[str, Any]:
    try:
        from Vera.vera.provenance import get_provenance
        return dict(get_provenance() or {})
    except Exception:
        return {}


async def _effective(minds: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """{modes, digest}: digest over the modes NOT at their default (what was switched)."""
    fn = (CAPABILITY_REGISTRY.get("minds.effective") or {}).get("func")
    if not fn:
        return {"modes": {}, "digest": ""}
    try:
        eff = await fn(minds=minds or None)
    except Exception as e:
        log.debug("memory perf: minds.effective: %s", e)
        return {"modes": {}, "digest": ""}
    modes, src = eff.get("modes") or {}, eff.get("source") or {}
    switched = {k: v for k, v in modes.items() if src.get(k, "default") != "default"}
    return {"modes": modes, "digest": SB.digest(switched)}


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in TABLE_DDL:
            await conn.execute(s)
        _DDL_DONE = True


async def _store(rows: List[Dict[str, Any]]) -> int:
    pool = _orch.PG_POOL
    if pool is None or not rows:
        return 0
    async with pool.acquire() as conn:
        await _ensure(conn)
        for r in rows:
            await conn.execute(
                "INSERT INTO memory_perf (kind, source, sha_short, minds_digest, calls, data) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                r["kind"], r["source"], r["sha_short"], r["minds_digest"], int(r["calls"]),
                json.dumps(r, default=str))
        if time.time() - _STATE["pruned"] > 86400:
            await conn.execute("DELETE FROM memory_perf WHERE ts < NOW() - make_interval(days => $1)", KEEP_DAYS)
            _STATE["pruned"] = time.time()
    return len(rows)


async def log_once() -> Dict[str, Any]:
    """One live row per source with calls since the last one."""
    async with _LOCK:
        return await _log_once_locked()


async def _log_once_locked() -> Dict[str, Any]:
    now = time.time()
    interval = now - float(_STATE["at"])
    prov, eff = _provenance(), await _effective()
    rows, marks = [], dict(_STATE["marks"])
    for source in SOURCES:
        w = _window(source)
        if w is None or not hasattr(w, "since"):
            continue
        paths, marks[source] = w.since(marks.get(source))
        r = MP.row("live", source, paths, prov, eff["modes"], eff["digest"], interval)
        if r:
            rows.append(r)
    stored = await _store(rows)
    if _orch.PG_POOL is not None:        # with no Postgres the calls stay unread until it is back
        _STATE["marks"], _STATE["at"] = marks, now
    _STATE["last"] = {"at": now, "stored": stored, "sources": [r["source"] for r in rows]}
    return {"ok": True, "stored": stored, "rows": [{"source": r["source"], "calls": r["calls"]} for r in rows]}


async def _log_job() -> None:
    try:
        await log_once()
    except Exception as e:
        log.warning("memory perf log: %s", e)


schedule(_log_job, interval=INTERVAL_S, name="memory_perf_log", singleton=True, skip_in_sandbox=True)


def _since(days: Any, default: int = 30) -> datetime:
    try:
        d = max(1, min(KEEP_DAYS, int(days)))
    except Exception:
        d = default
    return datetime.now(timezone.utc) - timedelta(days=d)


async def _fetch(kind: str = "", source: str = "", sha: str = "", digest: str = "", days: Any = 30,
                 limit: int = 2000) -> List[Dict[str, Any]]:
    pool = _orch.PG_POOL
    if pool is None:
        return []
    where, args = ["ts >= $1"], [_since(days)]
    for col, val in (("kind", kind), ("source", source), ("minds_digest", digest)):
        if val:
            args.append(str(val))
            where.append(f"{col} = ${len(args)}")
    if sha:
        args.append(str(sha)[:8] + "%")
        where.append(f"sha_short LIKE ${len(args)}")
    args.append(max(1, min(5000, int(limit))))
    sql = (f"SELECT ts, data FROM memory_perf WHERE {' AND '.join(where)} "
           f"ORDER BY ts DESC LIMIT ${len(args)}")
    async with pool.acquire() as conn:
        await _ensure(conn)
        recs = await conn.fetch(sql, *args)
    out = []
    for r in recs:
        d = r["data"]
        d = json.loads(d) if isinstance(d, str) else dict(d)
        d["ts"] = r["ts"].isoformat()
        out.append(d)
    return out


@capability(
    "memory.perf.versions", http_method="GET", http_path="/memory/perf/versions",
    contract=_MC.contract("memory.perf.versions", effects=["read"], idempotency="idempotent"),
    http_tags=["memory", "perf"], memory="off", silent=True,
    description="The memory performance log's versions (four-minds FM-7.9): every (code sha, switch digest, kind) "
                "with rows in the last `days`, newest first, with calls and first/last time - what "
                "memory.perf.compare can compare. Inputs: kind (live|probe, optional), days (int=30). Output: "
                "{versions: [{sha_short, minds_digest, kind, rows, calls, first, last, branch}], running}.",
)
async def cap_memory_perf_versions(kind: str = "", days: int = 30, trace_id=None) -> Dict[str, Any]:
    rows = await _fetch(kind=kind, days=days, limit=5000)
    return {"versions": MP.versions(rows), "running": MP.version(_provenance()), "last_log": dict(_STATE["last"])}


@capability(
    "memory.perf.rows", http_method="GET", http_path="/memory/perf/rows",
    contract=_MC.contract("memory.perf.rows", effects=["read"], idempotency="idempotent"),
    http_tags=["memory", "perf"], memory="off", silent=True,
    description="Memory performance rows (FM-7.9), newest first: each holds p50/p95 per path and stage with outcome "
                "counts for its calls, the sha that ran and the memory switch modes. Inputs: kind (live|probe), "
                "source (retrieval|chat), sha (prefix), digest, days (int=7), limit (int=100, max 2000). "
                "Output: {rows, count}.",
)
async def cap_memory_perf_rows(kind: str = "", source: str = "", sha: str = "", digest: str = "", days: int = 7,
                               limit: int = 100, trace_id=None) -> Dict[str, Any]:
    rows = await _fetch(kind, source, sha, digest, days, min(2000, max(1, int(limit))))
    return {"rows": rows, "count": len(rows)}


@capability(
    "memory.perf.compare", http_method="POST", http_path="/memory/perf/compare",
    contract=_MC.contract("memory.perf.compare", effects=["read"], idempotency="idempotent"),
    http_tags=["memory", "perf"], memory="off", silent=True,
    description="Did a change make memory slower? (FM-7.9) Pools the rows of a base and a head version and reports, "
                "per source|path, the p50/p95 change of the total and of each stage with a verdict (slower/faster "
                "past 15% and 20 ms with >= 20 calls, else same or too_few). Inputs: base_sha (prefix, required), "
                "head_sha (prefix, default the running code), kind (probe default | live), source (optional), "
                "base_digest/head_digest (optional switch digests), days (int=30). Output: {base, head, kind, "
                "headline, paths}.",
)
async def cap_memory_perf_compare(base_sha: str = "", head_sha: str = "", kind: str = "probe", source: str = "",
                                  base_digest: str = "", head_digest: str = "", days: int = 30,
                                  trace_id=None) -> Dict[str, Any]:
    if not base_sha:
        return {"ok": False, "error": "base_sha is required (memory.perf.versions lists them)"}
    head_sha = head_sha or MP.version(_provenance())["sha_short"]
    if not head_sha:            # an empty prefix would match every row and compare the base with everything
        return {"ok": False, "error": "head_sha is required here: this process does not know its own sha"}
    base = await _fetch(kind, source, base_sha, base_digest, days, 5000)
    head = await _fetch(kind, source, head_sha, head_digest, days, 5000)
    if not base or not head:
        return {"ok": False, "error": f"no {kind} rows for {'base' if not base else 'head'} in {days} days",
                "base_rows": len(base), "head_rows": len(head)}
    out = MP.compare(base, head)
    return dict(out, ok=True, kind=kind, base={"sha": base_sha, "rows": len(base), "digest": base_digest},
                head={"sha": head_sha, "rows": len(head), "digest": head_digest})


@capability(
    "memory.perf.probe", http_method="POST", http_path="/memory/perf/probe",
    contract=_MC.contract("memory.perf.probe", effects=["read", "model", "write"], idempotency="at_least_once"),
    http_tags=["memory", "perf"], memory="off",
    description="The memory census (FM-7.9): the frozen known-item set through memory.seek (cache off), its per-stage "
                "timings taken from the retrieval window, stored as a probe row tagged with the running sha and "
                "switch modes, beside the known-item quality (MRR, hit@10). Runs at night from a Loop Lab schedule "
                "entry; the same queries every time, so memory.perf.compare on probes is not moved by traffic mix. "
                "Inputs: name (str='v1'), limit (int=50, max 200), minds (dict, this run only), label (str). "
                "Output: {ok, stored, calls, total, quality, label}.",
)
async def cap_memory_perf_probe(name: str = "v1", limit: int = 50, minds: Optional[Dict[str, str]] = None,
                                label: str = "", trace_id=None) -> Dict[str, Any]:
    run = (CAPABILITY_REGISTRY.get("retrieval.known_items.run") or {}).get("func")
    w = _window("retrieval")
    if not run or w is None or not hasattr(w, "since"):
        return {"ok": False, "error": "retrieval.known_items.run or the retrieval window is not loaded"}
    async with _LOCK:
        await _log_once_locked()                  # live calls so far go to a live row first
        before = w.marks()
        res = await run(name=name, limit=max(1, min(200, int(limit))), minds=minds or None, label=label)
        paths, after = w.since(before)
        _STATE["marks"]["retrieval"] = after      # the probe's calls never reach a live row
    if not (res or {}).get("ok"):
        return {"ok": False, "error": (res or {}).get("error") or "known-item run failed"}
    paths = {p: s for p, s in paths.items() if p == "memory.seek"}
    summ = res.get("summary") or {}
    eff = await _effective(minds)
    r = MP.row("probe", "retrieval", paths, _provenance(), eff["modes"], eff["digest"],
               extra={"set": name, "label": res.get("label") or label, "items": summ.get("n"),
                      "quality": {k: summ.get(k) for k in ("mrr", "ndcg@10", "hit@1", "hit@10", "missing")},
                      "latency": {k: summ.get(k) for k in ("ms_p50", "ms_p95")},
                      "run_path": res.get("path")})
    stored = await _store([r] if r else [])
    seek = paths.get("memory.seek") or {}
    return {"ok": True, "stored": stored, "calls": seek.get("calls", 0), "total": seek.get("total"),
            "quality": (r or {}).get("extra", {}).get("quality"), "label": res.get("label")}


@capability(
    "memory.perf.log_now", http_method="POST", http_path="/memory/perf/log_now",
    contract=_MC.contract("memory.perf.log_now", effects=["write"], idempotency="at_least_once"),
    http_tags=["memory", "perf"], memory="off", silent=True,
    description="Write the memory performance live rows now instead of at the next 15-minute tick (FM-7.9) - e.g. "
                "just before a restart, which would otherwise drop the calls since the last row. Output: "
                "{ok, stored, rows}.",
)
async def cap_memory_perf_log_now(trace_id=None) -> Dict[str, Any]:
    return await log_once()
