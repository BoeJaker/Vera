"""chat.replay.*: the private chat replay store (four-minds FM-0.3; see chat_replay_core for why).

capture() is called by /agents/chat/stream with the request body and never makes the turn wait: the
switch is read and the row written in a background task, and any failure is logged and dropped.
Rows live in Postgres (`chat_replay`) for 30 days; chat.replay.purge deletes on request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import chat_replay_core as CR
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import chat_replay_core as CR          # type: ignore
    from vera.minds import contract_core as _MC            # type: ignore

log = logging.getLogger("vera.minds.chat_replay")
SWITCH = "chat.replay_capture"
MODE_TTL_S = 30.0
_DDL = (
    """CREATE TABLE IF NOT EXISTS chat_replay (
           id            BIGSERIAL PRIMARY KEY,
           ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           session_id    TEXT NOT NULL DEFAULT '',
           agent         TEXT NOT NULL DEFAULT '',
           message_chars INTEGER NOT NULL DEFAULT 0,
           digest        TEXT NOT NULL DEFAULT '',
           body          JSONB NOT NULL,
           approved_at   TIMESTAMPTZ,
           approved_by   TEXT NOT NULL DEFAULT '',
           note          TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS chat_replay_ts ON chat_replay (ts)",
    "CREATE INDEX IF NOT EXISTS chat_replay_session ON chat_replay (session_id)",
)
_DDL_DONE = False
_MODE: Dict[str, Any] = {"mode": "off", "at": 0.0}
_PENDING: set = set()
_STATS: Dict[str, int] = {"captured": 0, "skipped": 0, "dropped_no_db": 0, "errors": 0}


def _cred():
    try:
        from Vera.vera.log_setup import redact
        return redact
    except Exception:
        return None


async def _mode() -> str:
    """The switch's global mode, read at most every 30 s (a chat turn must not pay for it)."""
    if time.monotonic() - _MODE["at"] < MODE_TTL_S:
        return _MODE["mode"]
    mode = "off"
    try:
        eff = (CAPABILITY_REGISTRY.get("minds.effective") or {}).get("func")
        if eff:
            mode = str((((await eff()) or {}).get("modes") or {}).get(SWITCH) or "off")
    except Exception as e:
        log.debug("chat replay switch: %s", e)
    _MODE.update(mode=mode, at=time.monotonic())
    return mode


def capture(body: Optional[Dict[str, Any]], session_id: str) -> None:
    """Keep this turn's request body if the switch is on, in the background. Never raises."""
    try:
        if not isinstance(body, dict) or not body:
            return
        task = asyncio.get_running_loop().create_task(_capture(dict(body), str(session_id or "")))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        log.debug("chat replay capture: %s", e)


async def _ensure(conn) -> None:
    global _DDL_DONE
    if not _DDL_DONE:
        for s in _DDL:
            await conn.execute(s)
        _DDL_DONE = True


async def _capture(body: Dict[str, Any], session_id: str) -> None:
    try:
        if not CR.wanted(await _mode(), session_id):
            _STATS["skipped"] += 1
            return
        pool = _orch.PG_POOL
        if pool is None:
            _STATS["dropped_no_db"] += 1
            return
        r = CR.row(body, session_id, _cred())
        async with pool.acquire() as conn:
            await _ensure(conn)
            await conn.execute(
                "INSERT INTO chat_replay (session_id, agent, message_chars, digest, body) VALUES ($1, $2, $3, $4, $5::jsonb)",
                r["session_id"], r["agent"], r["message_chars"], r["digest"],
                json.dumps(dict(r["body"], _meta={"body_chars": r["body_chars"], "truncated": r["truncated"]}),
                           ensure_ascii=False, default=str))
        _STATS["captured"] += 1
    except Exception as e:
        _STATS["errors"] += 1
        log.debug("chat replay write: %s", e)


async def _retention_job() -> None:
    pool = _orch.PG_POOL
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await _ensure(conn)
            await conn.execute("DELETE FROM chat_replay WHERE ts < NOW() - make_interval(days => $1)", CR.RETAIN_DAYS)
    except Exception as e:
        log.warning("chat replay retention: %s", e)


schedule(_retention_job, interval=86400, name="chat_replay_retention", singleton=True, skip_in_sandbox=True)


def _body(v: Any) -> Dict[str, Any]:
    return json.loads(v) if isinstance(v, str) else dict(v or {})


async def _conn_or_error():
    pool = _orch.PG_POOL
    if pool is None:
        return None, {"ok": False, "error": "postgres unavailable"}
    return pool, None


@capability(
    "chat.replay.list", http_method="GET", http_path="/chat/replay/list",
    contract=_MC.contract("chat.replay.list", effects=["read"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off", silent=True,
    description="Captured chat turns (four-minds FM-0.3, switch chat.replay_capture), newest first: id, time, "
                "session, agent, a short preview of the message and whether it is approved for the corpus - never "
                "the whole body (chat.replay.get). Inputs: days (int=7, max 30), limit (int=50, max 500), "
                "session_id (str), approved ('' all | 'yes' | 'no'). Output: {cases, count, mode}.",
)
async def cap_chat_replay_list(days: int = 7, limit: int = 50, session_id: str = "", approved: str = "",
                               trace_id=None) -> Dict[str, Any]:
    pool, err = await _conn_or_error()
    if err:
        return err
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(CR.RETAIN_DAYS, int(days or 7))))
    where, args = ["ts >= $1"], [since]
    if session_id:
        args.append(str(session_id))
        where.append(f"session_id = ${len(args)}")
    if approved in ("yes", "no"):
        where.append("approved_at IS NOT NULL" if approved == "yes" else "approved_at IS NULL")
    args.append(max(1, min(500, int(limit or 50))))
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch(
            f"SELECT id, ts, session_id, agent, message_chars, approved_at, body->>'message' AS message "
            f"FROM chat_replay WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ${len(args)}", *args)
    cases = [{"id": r["id"], "ts": r["ts"].isoformat(), "session_id": r["session_id"], "agent": r["agent"],
              "message_chars": r["message_chars"], "preview": CR.preview(r["message"] or ""),
              "approved": r["approved_at"] is not None} for r in rows]
    return {"cases": cases, "count": len(cases), "mode": await _mode()}


@capability(
    "chat.replay.get", http_method="POST", http_path="/chat/replay/get",
    contract=_MC.contract("chat.replay.get", effects=["read"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off", silent=True,
    description="One captured chat turn in full: the request body as the browser sent it (credentials redacted), "
                "for replaying it. Input: id (int). Output: {ok, id, ts, session_id, agent, approved, body}.",
)
async def cap_chat_replay_get(id: int = 0, trace_id=None) -> Dict[str, Any]:
    pool, err = await _conn_or_error()
    if err:
        return err
    async with pool.acquire() as conn:
        await _ensure(conn)
        r = await conn.fetchrow("SELECT * FROM chat_replay WHERE id = $1", int(id or 0))
    if not r:
        return {"ok": False, "error": f"no captured turn {id}"}
    return {"ok": True, "id": r["id"], "ts": r["ts"].isoformat(), "session_id": r["session_id"], "agent": r["agent"],
            "approved": r["approved_at"] is not None, "approved_by": r["approved_by"], "note": r["note"],
            "body": _body(r["body"])}


@capability(
    "chat.replay.approve", http_method="POST", http_path="/chat/replay/approve",
    contract=_MC.contract("chat.replay.approve", effects=["write"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off",
    description="Approve (or withdraw) ONE captured chat turn for the chat corpus - the per-case approval the owner "
                "asked for (§13 Q3); nothing reaches the corpus without it. Inputs: id (int), approve (bool=true), "
                "by (str, who approved), note (str). Output: {ok, id, approved}.",
)
async def cap_chat_replay_approve(id: int = 0, approve: bool = True, by: str = "", note: str = "",
                                  trace_id=None) -> Dict[str, Any]:
    pool, err = await _conn_or_error()
    if err:
        return err
    async with pool.acquire() as conn:
        await _ensure(conn)
        if approve:
            res = await conn.execute("UPDATE chat_replay SET approved_at = NOW(), approved_by = $2, note = $3 "
                                     "WHERE id = $1", int(id or 0), str(by or ""), str(note or ""))
        else:
            res = await conn.execute("UPDATE chat_replay SET approved_at = NULL, approved_by = '', note = $2 "
                                     "WHERE id = $1", int(id or 0), str(note or ""))
    if not str(res).endswith(" 1"):
        return {"ok": False, "error": f"no captured turn {id}"}
    return {"ok": True, "id": int(id), "approved": bool(approve)}


@capability(
    "chat.replay.approved", http_method="GET", http_path="/chat/replay/approved",
    contract=_MC.contract("chat.replay.approved", effects=["read"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off", silent=True,
    description="The approved captured turns as corpus cases ({source: replay:<id>, message, agent_name, history, "
                "request, note}) - what the chat corpus may take in. Input: limit (int=100, max 500). Output: "
                "{cases, count}.",
)
async def cap_chat_replay_approved(limit: int = 100, trace_id=None) -> Dict[str, Any]:
    pool, err = await _conn_or_error()
    if err:
        return err
    async with pool.acquire() as conn:
        await _ensure(conn)
        rows = await conn.fetch("SELECT id, body, note FROM chat_replay WHERE approved_at IS NOT NULL "
                                "ORDER BY approved_at DESC LIMIT $1", max(1, min(500, int(limit or 100))))
    cases = [CR.corpus_case(r["id"], _body(r["body"]), r["note"]) for r in rows]
    return {"cases": cases, "count": len(cases)}


@capability(
    "chat.replay.purge", http_method="POST", http_path="/chat/replay/purge",
    contract=_MC.contract("chat.replay.purge", effects=["delete"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off",
    description="Delete captured chat turns: one (id), a whole session (session_id), or everything (all=true). "
                "Output: {ok, deleted}.",
)
async def cap_chat_replay_purge(id: int = 0, session_id: str = "", all: bool = False,
                                trace_id=None) -> Dict[str, Any]:
    if not (id or session_id or all):
        return {"ok": False, "error": "give id, session_id or all=true"}
    pool, err = await _conn_or_error()
    if err:
        return err
    async with pool.acquire() as conn:
        await _ensure(conn)
        if all:
            res = await conn.execute("DELETE FROM chat_replay")
        elif id:
            res = await conn.execute("DELETE FROM chat_replay WHERE id = $1", int(id))
        else:
            res = await conn.execute("DELETE FROM chat_replay WHERE session_id = $1", str(session_id))
    try:
        n = int(str(res).rsplit(" ", 1)[-1])
    except Exception:
        n = 0
    return {"ok": True, "deleted": n}


@capability(
    "chat.replay.stats", http_method="GET", http_path="/chat/replay/stats",
    contract=_MC.contract("chat.replay.stats", effects=["read"], idempotency="idempotent"),
    http_tags=["chat", "minds"], memory="off", silent=True,
    description="The chat replay store: the switch mode, cases kept and approved, the oldest, and this process's "
                "capture counters. Output: {mode, kept, approved, oldest, retain_days, process}.",
)
async def cap_chat_replay_stats(trace_id=None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"mode": await _mode(), "retain_days": CR.RETAIN_DAYS, "process": dict(_STATS)}
    pool = _orch.PG_POOL
    if pool is None:
        return dict(out, error="postgres unavailable")
    async with pool.acquire() as conn:
        await _ensure(conn)
        r = await conn.fetchrow("SELECT count(*) AS kept, count(approved_at) AS approved, min(ts) AS oldest "
                                "FROM chat_replay")
    return dict(out, kept=r["kept"], approved=r["approved"], oldest=r["oldest"].isoformat() if r["oldest"] else None)
