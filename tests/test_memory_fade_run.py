"""The fading job (memory.fade.run). Owner, 2026-10-05: apply the policy as is - with the user's controls and
resurfacing in place first. Soft-archive only, never during the nightly backup, the nightly run off unless switched on.
"""
import asyncio
import importlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _hooks():
    try:
        return importlib.import_module("Vera.vera.fabric.memory_hooks")
    except Exception:
        pytest.skip("the app package is not importable here (run in the gate container)")


NOW = datetime.now(timezone.utc)
ROWS = [
    {"id": "a-fresh", "record_type": "message", "source_type": "", "category": "", "importance": 0.5,
     "created_at": NOW - timedelta(days=1), "head": "hi"},
    {"id": "b-old", "record_type": "message", "source_type": "", "category": "", "importance": 0.5,
     "created_at": NOW - timedelta(days=400), "head": "old chat"},
    {"id": "c-tool", "record_type": "cap_call", "source_type": "", "category": "", "importance": 0.4,
     "created_at": NOW - timedelta(days=30), "head": "[board.items]"},
]


class _Conn:
    def __init__(self, faded=()):
        self.updated, self.faded = [], sorted(faded)       # faded: already archived with faded_at by an earlier run

    async def fetchval(self, sql, *a):
        return False                                       # no usage table yet

    async def fetch(self, sql, *a):
        if "faded_at" in sql and sql.lstrip().startswith("SELECT"):
            return [{"id": i} for i in self.faded if i > a[0]][:20000]
        if "id > $1" in sql:
            return [r for r in ROWS if r["id"] > a[0]][:5000]
        if sql.lstrip().startswith("UPDATE vera_memories"):
            ids, reasons = a
            self.updated.append(list(zip(ids, reasons)))
            return [{"id": i} for i in ids]
        return []


class _Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        c = self.c

        class _Ctx:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return _Ctx()


class _Redis:
    def __init__(self):
        self.kv = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v):
        self.kv[k] = v


class _NeoSession:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def run(self, cypher, params):
        self.log.append(list(params["ids"]))

        class _Res:
            async def single(s):
                return {"n": len(params["ids"])}
        return _Res()


class _Neo:
    def __init__(self):
        self.log = []
        self._driver = self

    def session(self):
        return _NeoSession(self.log)


class _Coll:
    def __init__(self, have):
        self.have, self.updates = set(have), []

    def get(self, ids=None, include=None, limit=None, offset=0):
        hit = [i for i in ids if i in self.have] if ids is not None else sorted(self.have)[offset:offset + limit]
        return {"ids": hit, "metadatas": [{"record_type": "message"} for _ in hit]}

    def update(self, ids, metadatas):
        self.updates.append((list(ids), metadatas))


def _wire(H, monkeypatch, coll_have=(), faded=()):
    conn, r, neo, coll = _Conn(faded), _Redis(), _Neo(), _Coll(coll_have)

    async def pool():
        return _Pool(conn)

    class _Chroma:
        _collection = coll

    class _Mem:
        _backends = {"neo4j": neo, "chroma": _Chroma()}
        _FADED_SEEN = {"any": 0.0, "t": 99.0}
    monkeypatch.setattr(H, "_usage_pool", pool)
    monkeypatch.setattr(H, "_redis", lambda: r)
    monkeypatch.setattr(H, "_memory", lambda: (_Mem(), None))
    monkeypatch.setattr(H, "_FADE_CHROMA_PAUSE_S", 0.0)
    return conn, r, neo, coll, _Mem


def test_the_backup_window_is_respected():
    H = _hooks()
    mk = lambda h, m: time.struct_time((2026, 10, 5, h, m, 0, 0, 278, 1))
    assert H._in_backup_window(mk(1, 0)) and H._in_backup_window(mk(3, 29))
    assert not H._in_backup_window(mk(0, 59)) and not H._in_backup_window(mk(3, 30)) and not H._in_backup_window(mk(4, 0))


def test_a_run_soft_archives_what_fades_in_every_store_and_records_why(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, Mem = _wire(H, monkeypatch, coll_have={"b-old"})
    out = _arun(H._fade_apply(100, None, "test"))
    ids = [i for chunk in conn.updated for i, _r in chunk]
    assert sorted(ids) == ["b-old", "c-tool"]                         # the fresh one stays
    reasons = dict(i for chunk in conn.updated for i in chunk)
    assert reasons["b-old"].startswith("conversation: strength") and "below floor" in reasons["b-old"]
    assert out["faded"] == 2 and out["neo4j"] == 2 and out["chroma"] == 1
    assert coll.updates[0][0] == ["b-old"] and coll.updates[0][1][0]["archived"] is True
    assert coll.updates[0][1][0]["record_type"] == "message"          # the rest of the vector's metadata kept
    assert Mem._FADED_SEEN["t"] == 0.0                                 # recall looks for faded matches at once
    assert json.loads(r.kv[H._FADE_RUN_KEY])["faded"] == 2


def test_a_class_the_user_keeps_is_never_faded(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch)
    r.kv[H._FADE_POLICY_KEY] = json.dumps({"overrides": {"tool": {"enabled": False}}})
    _arun(H._fade_apply(100, None, "test"))
    assert [i for chunk in conn.updated for i, _r in chunk] == ["b-old"]


def test_without_confirm_nothing_changes_and_in_the_backup_window_nothing_starts(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch)
    monkeypatch.setattr(H, "_FADE_RUN_TASK", None)
    fn = getattr(H.memory_fade_run, "__wrapped__", H.memory_fade_run)
    dry = _arun(fn())
    assert dry["dry"] is True and conn.updated == []
    monkeypatch.setattr(H, "_in_backup_window", lambda t=None: True)
    assert "refused" in _arun(fn(confirm=True)) and conn.updated == []


def test_nightly_fading_is_off_until_switched_on(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch)
    pol = getattr(H.memory_fade_policy, "__wrapped__", H.memory_fade_policy)
    assert _arun(pol())["auto"] is False and _arun(H._fade_auto_on()) is False
    assert _arun(pol(auto=True, by="owner"))["auto"] is True and _arun(H._fade_auto_on()) is True


def test_a_run_cut_short_is_levelled_by_the_next(monkeypatch):
    """2026-10-06: a restart stopped run 1 after Postgres had 28,000 faded and before Neo4j or Chroma heard of them."""
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch, coll_have={"z-left", "y-live"}, faded={"z-left", "x-left"})
    out = _arun(H._fade_apply(100, None, "test"))
    assert out["reconciled"]["faded_in_postgres"] == 2
    assert sorted(neo.log[0]) == ["x-left", "z-left"]                 # Neo4j told first
    assert coll.updates[0][0] == ["z-left"]                           # only the faded vector, not the live one
    assert out["reconciled"]["chroma"] == 1


def test_each_batch_reaches_every_store_and_the_backup_window_stops_the_run(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch, coll_have={"b-old", "c-tool"})
    monkeypatch.setattr(H, "_FADE_BATCH", 1)
    calls = iter([False, True])
    monkeypatch.setattr(H, "_in_backup_window", lambda t=None: next(calls, True))
    out = _arun(H._fade_apply(100, None, "test"))
    assert out["faded"] == 1 and out["neo4j"] == 1 and out["chroma"] == 1   # the one batch done is done everywhere
    assert "backup" in out["stopped"] and out["picked"] == 2
    assert json.loads(r.kv[H._FADE_RUN_KEY])["running"] is False


def test_a_run_a_restart_stopped_says_so(monkeypatch):
    H = _hooks()
    conn, r, neo, coll, _M = _wire(H, monkeypatch)
    monkeypatch.setattr(H, "_FADE_RUN_TASK", None)
    r.kv[H._FADE_RUN_KEY] = json.dumps({"faded": 28000, "running": True})
    fn = getattr(H.memory_fade_run, "__wrapped__", H.memory_fade_run)
    assert "interrupted" in _arun(fn())["last_run"]