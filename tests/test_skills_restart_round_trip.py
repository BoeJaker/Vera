"""S10 / D13-3: a user skill keeps its edits and its applies_to_caps across a restart, and a deleted one stays deleted.

_startup_load used to load the fabric copy first. That copy is written once at create time without the list
fields, so every user skill came back as its creation-time version without applies_to_caps (and with it the
loop's auto-attach), and deleted skills returned. Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import sqlite3
import sys
import types

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.skills.skills as sk  # noqa: E402  (fails loudly, never skips)


def _fake_fabric():
    rows = {}

    async def ingest_dataset(dataset_id="", data=None, source="", tags=None, **_):
        rows.setdefault(dataset_id, []).extend(dict(d) for d in (data or []))

    async def query_dataset(dataset_id="", query=None, **_):
        return [{"data": d} for d in rows.get(dataset_id, [])]          # oldest first

    return types.SimpleNamespace(ingest_dataset=ingest_dataset, query_dataset=query_dataset, rows=rows)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(sk, "_SQLITE_PATH", tmp_path / "skills.db")
    sk._sqlite_init().close()
    monkeypatch.setattr(sk, "_redis", lambda: None)
    fab = _fake_fabric()
    monkeypatch.setitem(sys.modules, "data_fabric", fab)
    monkeypatch.setattr(sk, "SKILLS", {})
    monkeypatch.setattr(sk, "ONTOLOGIES", {})
    return fab


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


async def _restart():
    sk.SKILLS.clear()
    sk.ONTOLOGIES.clear()
    await sk._startup_load()


def _call(cap, **kw):
    fn = getattr(cap, "__wrapped__", cap)
    return fn(**kw)


def test_edit_and_caps_survive_and_a_delete_stays_deleted(store):
    async def run():
        a = await _call(sk.skills_create, name="probe-a", content="first", applies_to_caps="web.search,memory.seek")
        b = await _call(sk.skills_create, name="probe-b", content="gone soon")
        await _drain()
        await _call(sk.skills_update, id=a["id"], content="edited")
        await _call(sk.skills_delete, id=b["id"])
        await _drain()
        assert len(store.rows["skills"]) >= 3          # the fabric holds the stale create rows and b's
        await _restart()
        return a["id"], b["id"]

    a_id, b_id = asyncio.run(run())
    assert sk.SKILLS[a_id]["content"] == "edited"
    assert sk.SKILLS[a_id]["applies_to_caps"] == ["web.search", "memory.seek"]
    assert b_id not in sk.SKILLS


def test_the_fabric_still_restores_a_skill_missing_from_sqlite(store):
    async def run():
        a = await _call(sk.skills_create, name="probe-c", content="first")
        await _drain()
        await _call(sk.skills_update, id=a["id"], content="edited")
        await _drain()
        conn = sqlite3.connect(str(sk._SQLITE_PATH))
        conn.execute("DELETE FROM skills WHERE id=?", (a["id"],))       # a lost row, not a user delete
        conn.commit()
        conn.close()
        await _restart()
        return a["id"]

    a_id = asyncio.run(run())
    assert sk.SKILLS[a_id]["content"] == "edited"       # the newest fabric row wins


def test_a_recreated_id_is_no_longer_tombstoned(store):
    sk._sqlite_delete("skills", "x1")
    assert "x1" in sk._sqlite_tombstones("skills")
    sk._sqlite_save("skills", {"id": "x1", "name": "back"})
    assert "x1" not in sk._sqlite_tombstones("skills")
