"""Start-up schema work never waits on a lock.

2026-10-05: prod restarted during the nightly pg_dump. The memory store's connect ran a no-op
ALTER TABLE vera_memory_edges ADD COLUMN IF NOT EXISTS, which waits for an exclusive lock behind the dump's share
locks, and every read of the table queued behind it: memory came up 8 minutes late (Neo4j absent from recall until
then); the fabric's connect wedged the same way on fabric_datasets. Proven on the real Postgres: with lock_timeout
3s the same ALTER is cancelled after 3.16 s instead of waiting.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_memory_connect_sets_a_lock_timeout_before_its_schema_work():
    s = _src("vera/fabric/memory.py")
    a = s.index("self._pool = await _asyncpg.create_pool(POSTGRES_URL, min_size=2, max_size=10)")
    first = s.index("await conn.execute(", a)
    assert s[first:first + 80].startswith("await conn.execute(f\"SET lock_timeout = '{_PG_STARTUP_LOCK_TIMEOUT}'\")")


def test_no_bare_index_creation_can_fail_the_memory_connect():
    s = _src("vera/fabric/memory.py")
    a = s.index("# Step 4: Create unique index")
    b = s.index("PostgresBackend connected", a)
    assert not re.search(r'await conn\.execute\("CREATE INDEX', s[a:b])
    assert len(re.findall(r'await _pg_try\(conn, "CREATE INDEX IF NOT EXISTS', s[a:b])) == 10


def test_the_fabric_connect_sets_a_lock_timeout_too():
    s = _src("vera/fabric/data_fabric.py")
    a = s.rindex("CREATE TABLE IF NOT EXISTS fabric_datasets")   # the Postgres store (the first is the SQLite schema)
    head = s[a - 600:a]
    assert "SET lock_timeout = '" in head and "VERA_PG_STARTUP_LOCK_TIMEOUT" in head
