"""D10-3: one row the database keeps refusing no longer blocks the decision ledger; a database outage counts nothing.

drain_once moves rows from the Redis stream into Postgres, pending rows first. A row that raised on insert stayed
pending and was redelivered first on every drain, so nothing behind it ever drained. Now, while the database still
answers, a row refused POISON_TRIES times moves to vera:decide:ledger:dead and the rest drain. Also: the ledger DDL
carries the D10-3 indexes. Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import json
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.minds.ledger_capabilities as L  # noqa: E402  (fails loudly, never skips)


class FakeStreams:
    """XADD / XGROUP CREATE / XREADGROUP (0 = own pending, > = new) / XACK / XPENDING range, with delivery counts."""

    def __init__(self):
        self.s, self.pending, self.seq = {}, {}, 0

    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self.seq += 1
        eid = f"{self.seq}-0".encode()
        self.s.setdefault(name, []).append((eid, {k.encode(): str(v).encode() for k, v in fields.items()}))
        return eid

    async def xgroup_create(self, name, group, id="0", mkstream=False):
        if getattr(self, "_grp", False):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self._grp, self.delivered = True, set()

    async def xreadgroup(self, group, consumer, streams, count=None):
        (name, start), = streams.items()
        if start == "0":
            ids = [e for e in self.s.get(name, []) if e[0] in self.pending]
        else:
            ids = [e for e in self.s.get(name, []) if e[0] not in self.delivered]
        ids = ids[:count]
        for eid, _ in ids:
            self.delivered.add(eid)
            self.pending[eid] = self.pending.get(eid, 0) + 1
        return [[name.encode(), ids]] if ids else []

    async def xack(self, name, group, *ids):
        for i in ids:
            self.pending.pop(i, None)

    async def xpending_range(self, name, group, min, max, count, consumername=None, idle=None):
        n = self.pending.get(min)
        return [{"message_id": min, "times_delivered": n}] if n else []


class FakeConn:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("INSERT INTO DECISION_LEDGER"):
            if not self.db.up:
                raise ConnectionError("connection refused")
            if args[0] in self.db.poison:
                raise ValueError("invalid input syntax for type json")
            self.db.rows.append(args[0])
        elif not self.db.up:
            raise ConnectionError("connection refused")
        else:
            self.db.ddl.append(sql)

    async def fetchval(self, sql):
        if not self.db.up:
            raise ConnectionError("connection refused")
        return 1


class FakePool:
    def __init__(self):
        self.up, self.poison, self.rows, self.ddl = True, set(), [], []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(pool)

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.fixture
def world(monkeypatch):
    r, pool = FakeStreams(), FakePool()
    monkeypatch.setattr(L._orch, "REDIS", r)
    monkeypatch.setattr(L._orch, "PG_POOL", pool)
    monkeypatch.setattr(L, "_DDL_DONE", False)
    monkeypatch.setattr(L, "_PARTITIONS", set())
    monkeypatch.setattr(L, "_problems", lambda row: [])          # rows are well-formed: only the DB refuses
    monkeypatch.setattr(L, "_ensure_partition", lambda conn, ts: asyncio.sleep(0))
    for i in range(4):
        run(r.xadd(L.STREAM, {"kind": "decision", "row": json.dumps(
            {"decision_id": f"d{i}", "ts": "2026-10-18T10:00:00+00:00", "mode": "live"})}))
    return r, pool


def run(c):
    return asyncio.run(c)


def test_a_refused_row_moves_aside_after_five_tries_and_the_rest_drain(world):
    r, pool = world
    pool.poison = {"d1"}
    for _ in range(L.POISON_TRIES - 1):
        run(L.drain_once())
    assert pool.rows == ["d0"]                              # held: d1 is retried, d2/d3 wait behind it
    assert not r.s.get(L.DEAD_STREAM)
    run(L.drain_once())                                      # fifth delivery: moved aside
    run(L.drain_once())
    assert pool.rows == ["d0", "d2", "d3"]
    dead = r.s[L.DEAD_STREAM]
    assert len(dead) == 1 and b"d1" in dead[0][1][b"row"] and dead[0][1][b"tries"] == b"5"
    assert not r.pending                                     # nothing left pending


def test_a_database_outage_counts_nothing_and_drops_nothing(world):
    r, pool = world
    pool.up = False
    for _ in range(L.POISON_TRIES + 3):
        with pytest.raises(Exception):
            run(L.drain_once())
    assert not r.s.get(L.DEAD_STREAM) and pool.rows == []
    pool.up = True
    run(L.drain_once())
    assert pool.rows == ["d0", "d1", "d2", "d3"] and not r.s.get(L.DEAD_STREAM)


def test_the_ledger_ddl_has_the_d10_3_indexes():
    ddl = " ".join(L.DDL)
    assert "decision_ledger_shadow_for ON decision_ledger ((row->'detail'->>'for')) WHERE mode = 'shadow'" in ddl
    assert "decision_ledger_run ON decision_ledger (run_id)" in ddl
    assert "decision_ledger_mode_question_ts ON decision_ledger (mode, question_id, ts)" in ddl
