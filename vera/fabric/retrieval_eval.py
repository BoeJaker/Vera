"""Seeks and reads into the retrieval eval set (four-minds FM-7.0); see retrieval_eval_core.

on_seek / on_read never raise and never make the caller wait: rows go to the decision ledger as
background tasks. The query text is kept out of the ledger row (which holds only its hash) in
its own small table, retrieval_eval_queries, keyed by that hash - purgeable on its own.
`retrieval.eval` reports use rate, hit@1, hit@3 and MRR over a window.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import capability

try:
    from Vera.vera.fabric import retrieval_eval_core as REC
    from Vera.vera.minds import ledger_core as LC
except Exception:                       # worktree / app-free import
    from vera.fabric import retrieval_eval_core as REC  # type: ignore
    from vera.minds import ledger_core as LC  # type: ignore

log = logging.getLogger("vera.retrieval_eval")
RECENT = REC.RecentSeeks()            # this process only: the fallback when Redis is absent
_PENDING: set = set()
# Seeks and reads can be served by different processes, so the recent seeks a read is credited
# against live in Redis (a capped list), and a (seek, record) pair is credited once (SET NX).
KEY_RECENT = "vera:reval:recent"
KEY_CREDIT = "vera:reval:credited:"
RECENT_CAP = 300
_DDL = """CREATE TABLE IF NOT EXISTS retrieval_eval_queries (
              state_hash TEXT PRIMARY KEY, query TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT NOW())"""


def _orch():
    return sys.modules.get("Vera.vera.capability_orchestration")


def _bg(coro) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro)
        _PENDING.add(t)
        t.add_done_callback(_PENDING.discard)
    except Exception:
        coro.close()


async def _append(row: Dict[str, Any]) -> None:
    try:
        try:
            from Vera.vera.minds import ledger_capabilities as L
        except Exception:
            from vera.minds import ledger_capabilities as L  # type: ignore
        await L.append(row)
    except Exception as e:
        log.debug("retrieval eval append: %s", e)


async def _store_query(state_hash: str, query: str) -> None:
    pool = getattr(_orch(), "PG_POOL", None)
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute("INSERT INTO retrieval_eval_queries (state_hash, query) VALUES ($1, $2) "
                               "ON CONFLICT (state_hash) DO NOTHING", state_hash, str(query)[:500])
    except Exception as e:
        log.debug("retrieval eval query store: %s", e)


def _origin() -> str:
    try:
        from Vera.vera.minds import loop_decisions_core as LD
    except Exception:
        try:
            from vera.minds import loop_decisions_core as LD  # type: ignore
        except Exception:
            return "user"
    ck = getattr(_orch(), "CALLER_KIND", None)
    return LD.origin_for(ck.get("") if ck is not None else "")


def on_seek(query: str, ids: List[str]) -> None:
    """A seek that returned records: a ranking decision. Never raises."""
    try:
        if not ids:
            return
        d = REC.seek_decision(query, ids, origin=_origin())
        RECENT.add(d["decision_id"], d["answer"])
        _bg(_append(d))
        _bg(_store_query(d["state_hash"], query))
        _bg(_remember(d["decision_id"], d["answer"]))
    except Exception as e:
        log.debug("retrieval eval seek: %s", e)


_CHAT: Dict[str, Any] = {}               # session_id -> (query, ranked ids) of the turn in flight


def on_chat_recall(session_id: str, query: str, ids: List[str]) -> None:
    """A chat turn was given these memories, best first. Kept until the turn's usage is judged
    (take_chat); nothing is recorded yet. Never raises."""
    try:
        if session_id and ids:
            _CHAT[str(session_id)] = (str(query or ""), [str(i) for i in ids][:REC.MAX_IDS])
            if len(_CHAT) > 500:              # a turn that never finished does not hold memory forever
                for k in list(_CHAT)[:100]:
                    _CHAT.pop(k, None)
    except Exception as e:
        log.debug("retrieval eval chat recall: %s", e)


def take_chat(session_id: str):
    """The pending chat recall for this session, removed; None when there is none."""
    try:
        return _CHAT.pop(str(session_id or ""), None)
    except Exception:
        return None


def on_chat_used(session_id: str, pending, used_ids: List[str]) -> None:
    """The turn counted toward use: record its ranking decision and an outcome per used memory.
    Never raises, never waits."""
    try:
        if not pending:
            return
        query, ranked = pending
        if not ranked:
            return
        d = REC.chat_recall_decision(query, ranked, origin=_origin(), session_id=session_id)
        _bg(_append_all([d] + REC.chat_used_outcomes(d["decision_id"], ranked, used_ids)))
        _bg(_store_query(d["state_hash"], query))
    except Exception as e:
        log.debug("retrieval eval chat used: %s", e)


async def _append_all(rows: List[Dict[str, Any]]) -> None:
    for row in rows:                        # the decision first, then its outcomes
        await _append(row)


def on_read(record_id: str) -> None:
    """A read of a record a recent seek ranked: the seek's outcome. Never raises."""
    try:
        _bg(_credit(str(record_id or "")))
    except Exception as e:
        log.debug("retrieval eval read: %s", e)


async def _remember(decision_id: str, ids: List[str]) -> None:
    r = getattr(_orch(), "REDIS", None)
    if r is None:
        return
    try:
        import time as _t
        await r.lpush(KEY_RECENT, json.dumps({"did": decision_id, "ids": ids, "ts": _t.time()}))
        await r.ltrim(KEY_RECENT, 0, RECENT_CAP - 1)
    except Exception as e:
        log.debug("retrieval eval remember: %s", e)


async def _credit(record_id: str) -> None:
    """Credit the newest seek in the window that ranked this record and has not been credited for it."""
    if not record_id:
        return
    r = getattr(_orch(), "REDIS", None)
    if r is None:
        hit = RECENT.match(record_id)
        if hit:
            await _append(REC.read_outcome(hit[0], hit[1]))
        return
    try:
        import time as _t
        raw = await r.lrange(KEY_RECENT, 0, RECENT_CAP - 1) or []
        entries = []
        for x in raw:
            try:
                entries.append(json.loads(x if isinstance(x, str) else x.decode()))
            except Exception:
                continue
        for did, rank in REC.pick_seeks(entries, record_id, _t.time()):
            if await r.set(KEY_CREDIT + did + "|" + record_id, "1", nx=True, ex=int(REC.RECENT_WINDOW_S) * 2):
                await _append(REC.read_outcome(did, rank))
                return
    except Exception as e:
        log.debug("retrieval eval credit: %s", e)


@capability(
    "retrieval.eval", http_method="GET", http_path="/retrieval/eval", http_tags=["memory"],
    memory="off", silent=True,
    description="The retrieval eval set from implicit labels (four-minds FM-7.0). kind=seek: every "
                "memory.seek is a ranking decision on the ledger, and a memory.read of a record it ranked "
                "(within 30 min) marks it used at that rank. kind=chat: the memories a counted chat turn "
                "was given, in rank order, and the ones its reply drew on. Output: {kind, summary: {seeks, "
                "used, use_rate, hit@1, hit@3, mrr}, examples: [{query, ranked, used_ranks}]}. Input: kind "
                "(seek | chat, default seek), days (int, default 7), examples (int, default 20).",
)
async def cap_retrieval_eval(days: int = 7, examples: int = 20, kind: str = "seek",
                             trace_id=None) -> Dict[str, Any]:
    pool = getattr(_orch(), "PG_POOL", None)
    if pool is None:
        return {"error": "postgres unavailable"}
    try:
        days = max(1, min(90, int(days)))
        examples = max(0, min(200, int(examples)))
    except Exception:
        days, examples = 7, 20
    qid = REC.CHAT_Q if str(kind or "").strip().lower() == "chat" else REC.SEEK_Q
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        rows = await conn.fetch(
            """SELECT l.decision_id, l.row, q.query,
                      COALESCE(array_agg((o.row->'detail'->>'rank')::int) FILTER (WHERE o.decision_id IS NOT NULL), '{}') AS ranks
               FROM decision_ledger l
               LEFT JOIN decision_outcomes o ON o.decision_id = l.decision_id AND o.source = $3
               LEFT JOIN retrieval_eval_queries q ON q.state_hash = l.row->>'state_hash'
               WHERE l.question_id = $1 AND l.ts >= $2
               GROUP BY l.decision_id, l.row, q.query, l.ts ORDER BY l.ts DESC""",
            qid, since, REC.USED_SOURCE[qid])
    seeks = []
    for r in rows:
        row = json.loads(r["row"]) if isinstance(r["row"], str) else r["row"]
        seeks.append({"decision_id": r["decision_id"], "k": len(row.get("answer") or []),
                      "used_ranks": sorted(int(x) for x in (r["ranks"] or []) if x is not None),
                      "query": (r["query"] or "")[:160], "ranked": (row.get("answer") or [])[:10]})
    return {"kind": "chat" if qid == REC.CHAT_Q else "seek", "summary": REC.summarize(seeks), "days": days,
            "examples": [{k: s[k] for k in ("query", "ranked", "used_ranks")} for s in seeks[:examples]]}
