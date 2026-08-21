"""Regression guard for ``data_fabric.query_dataset``.

``query_dataset`` is the dataset-read function ~13 call sites across agents,
skills, images, spritegen and character depend on to hydrate themselves from
the durable fabric. It was once *entirely missing* — every fabric-load silently
caught ``AttributeError: module 'data_fabric' has no attribute 'query_dataset'``
and returned nothing, so agents/skills/etc. failed to load from the durable
store. This pins the contract those callers rely on:
``query_dataset(dataset_id, {"limit", "offset", "include_data", "filter"})``
returning ``[{"id", "data", ...}]`` newest-first.

Reads only — no write-queue involved — so it's fully deterministic. It redirects
the module's SQLite path at a temp DB (``_sqlite_conn`` reads the module-global
``SQLITE_PATH`` at call time) and never touches a real fabric.
"""
import asyncio
import json
import sqlite3

import pytest

from vera.fabric import data_fabric as df

pytestmark = pytest.mark.critical


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fabric_records ("
            "id TEXT PRIMARY KEY, dataset_id TEXT, text TEXT, data TEXT, "
            "source_id TEXT, tags TEXT, created_at TEXT, synced_pg INTEGER DEFAULT 0)"
        )
        rows = [
            ("r1", "testds", "a", json.dumps({"k": 1, "name": "one"}), "s", "[]", "2026-01-01"),
            ("r2", "testds", "b", json.dumps({"k": 2, "name": "two"}), "s", "[]", "2026-01-02"),
            ("x1", "otherds", "c", json.dumps({"k": 9}), "s", "[]", "2026-01-03"),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO fabric_records VALUES (?,?,?,?,?,?,?,0)", rows
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def seeded_fabric(tmp_path, monkeypatch):
    db = str(tmp_path / "fabric_test.db")
    # _sqlite_conn() reads the module-global SQLITE_PATH at call time, so
    # redirecting it points every read at our writable temp DB (and reverts).
    monkeypatch.setattr(df, "SQLITE_PATH", db)
    _seed(db)
    return db


def test_returns_rows_and_decodes_data(seeded_fabric):
    rows = asyncio.run(df.query_dataset("testds", {"limit": 10, "include_data": True}))
    assert len(rows) == 2
    assert [r["id"] for r in rows] == ["r2", "r1"]  # newest-first by created_at
    assert isinstance(rows[0]["data"], dict)
    assert rows[0]["data"]["name"] == "two"


def test_filter_by_top_level_column(seeded_fabric):
    rows = asyncio.run(df.query_dataset("testds", {"filter": {"id": "r1"}}))
    assert [r["id"] for r in rows] == ["r1"]


def test_filter_by_data_payload_field(seeded_fabric):
    rows = asyncio.run(df.query_dataset("testds", {"filter": {"name": "two"}}))
    assert [r["id"] for r in rows] == ["r2"]


def test_include_data_false_drops_payload(seeded_fabric):
    rows = asyncio.run(df.query_dataset("testds", {"include_data": False}))
    assert rows and "data" not in rows[0]


def test_empty_dataset_id_reads_all(seeded_fabric):
    rows = asyncio.run(df.query_dataset("", {"limit": 100}))
    assert len(rows) >= 3


def test_unknown_dataset_is_empty(seeded_fabric):
    rows = asyncio.run(df.query_dataset("does-not-exist", {"limit": 10}))
    assert rows == []


def test_missing_query_arg_uses_defaults(seeded_fabric):
    # callers occasionally pass no query dict at all
    rows = asyncio.run(df.query_dataset("testds"))
    assert len(rows) == 2
    assert isinstance(rows[0]["data"], dict)  # include_data defaults True
