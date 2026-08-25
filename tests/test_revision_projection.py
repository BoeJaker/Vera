import json
import sqlite3

import pytest

from vera.fabric.record_revision import create_record_revision
from vera.fabric.revision_projection import apply_sqlite_projection


pytestmark = pytest.mark.critical
NOW = "2026-01-01T00:00:00Z"


def database(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE fabric_datasets (
                dataset_id TEXT PRIMARY KEY, record_count INTEGER DEFAULT 0,
                created_at TEXT, updated_at TEXT);
            CREATE TABLE fabric_records (
                id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, text TEXT, data TEXT,
                source_id TEXT, tags TEXT, created_at TEXT, synced_pg INTEGER DEFAULT 0);
        """)
    return path


def revision(value, *, parents=(), tombstone=False):
    return create_record_revision(
        namespace="test.dataset", record_type="document", logical_key="one",
        content=None if tombstone else {"value": value},
        created_at=NOW, parents=parents, tombstone=tombstone,
        metadata={"tags": ["canonical"]}).to_dict()


def test_projection_is_idempotent_and_retains_canonical_envelope(tmp_path):
    path = database(tmp_path)
    value = revision(1)
    first = apply_sqlite_projection(path, value, now=lambda: NOW)
    apply_sqlite_projection(path, value, now=lambda: NOW)
    assert first["state"] == "applied"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT record_count FROM fabric_datasets").fetchone()[0] == 1
        row = conn.execute("SELECT text,data,tags FROM fabric_records").fetchone()
    assert row[0] == '{"value":1}'
    assert json.loads(row[1])["revision_id"] == value["revision_id"]
    assert json.loads(row[2]) == ["canonical"]


def test_tombstone_removes_projection_and_decrements_once(tmp_path):
    path = database(tmp_path)
    value = revision(1)
    apply_sqlite_projection(path, value, now=lambda: NOW)
    deleted = revision(2, parents=[value["revision_id"]], tombstone=True)
    assert apply_sqlite_projection(path, deleted, now=lambda: NOW)["state"] == "removed"
    apply_sqlite_projection(path, deleted, now=lambda: NOW)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fabric_records").fetchone()[0] == 0
        assert conn.execute("SELECT record_count FROM fabric_datasets").fetchone()[0] == 0


def test_projection_failure_rolls_back_partial_legacy_write(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE fabric_datasets")
    with pytest.raises(sqlite3.OperationalError):
        apply_sqlite_projection(path, revision(1), now=lambda: NOW)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fabric_records").fetchone()[0] == 0
