"""S6 / D17-3: the agent Postgres mirror passes datetimes for its TIMESTAMPTZ columns.

AgentRegistry.save passed the record's ISO strings ('...Z') as $30/$31::TIMESTAMPTZ; asyncpg refuses a str there, so
every mirror write failed (4,512 'AgentRegistry PG save' errors in 25 h). Runs in the gate container."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.agents.agents as ag  # noqa: E402  (fails loudly, never skips)


class FakePool:
    def __init__(self):
        self.calls = []
        pool = self

        class _Conn:
            async def execute(self, sql, *args):
                pool.calls.append((sql, args))

        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *a):
                return False
        self._ctx = _Ctx

    def acquire(self):
        return self._ctx()


def test_the_mirror_insert_gets_aware_datetimes(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(ag, "_pg", lambda: pool)
    monkeypatch.setattr(ag, "_redis", lambda: None)
    rec = ag.AgentRecord(id="s6-agent", name="s6-agent")
    rec.created_at = "2026-10-18T09:15:00Z"
    asyncio.run(ag.AgentRegistry().save(rec))
    inserts = [a for sql, a in pool.calls if "INSERT INTO vera_agents" in sql]
    assert inserts, pool.calls
    args = inserts[0]
    assert len(args) == 33
    created, updated = args[29], args[30]                    # $30, $31
    assert isinstance(created, datetime) and created.tzinfo is not None
    assert created == datetime(2026, 10, 18, 9, 15, tzinfo=timezone.utc)
    assert isinstance(updated, datetime) and updated.tzinfo is not None


@pytest.mark.parametrize("value, ok", [("2026-10-18T09:15:00Z", True), ("2026-10-18T09:15:00+01:00", True),
                                       ("2026-10-18T09:15:00", True), ("", False), ("not a date", False)])
def test_pg_ts(value, ok):
    d = ag._pg_ts(value)
    assert isinstance(d, datetime) and d.tzinfo is not None
    if ok:
        assert d.year == 2026 and d.minute == 15
