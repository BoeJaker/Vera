import sqlite3

import pytest

from Vera.vera.fabric.revision_state_paths import (
    migrate_legacy_memory_revision_state,
)


def _legacy_database(path):
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE revisions (id TEXT PRIMARY KEY, body TEXT)")
        database.execute("INSERT INTO revisions VALUES (?, ?)", ("r1", "preserved"))


def test_revision_state_migration_is_digest_bound_and_non_destructive(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "state" / "canonical.sqlite3"
    _legacy_database(source)

    first = migrate_legacy_memory_revision_state(
        legacy_path=source, canonical_path=target)
    second = migrate_legacy_memory_revision_state(
        legacy_path=source, canonical_path=target)

    assert first["status"] == "ready"
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert not target.exists()

    with pytest.raises(ValueError, match="digest does not match"):
        migrate_legacy_memory_revision_state(
            legacy_path=source, canonical_path=target, dry_run=False,
            expected_snapshot_sha256="sha256:" + "0" * 64,
        )
    assert source.exists()
    assert not target.exists()

    result = migrate_legacy_memory_revision_state(
        legacy_path=source, canonical_path=target, dry_run=False,
        expected_snapshot_sha256=first["snapshot_sha256"],
    )
    assert result["status"] == "migrated"
    assert result["restart_required"] is True
    assert result["source_retained"] is True
    with sqlite3.connect(target) as database:
        assert database.execute(
            "SELECT id, body FROM revisions").fetchall() == [("r1", "preserved")]


def test_revision_state_migration_refuses_existing_target(tmp_path):
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "canonical.sqlite3"
    _legacy_database(source)
    target.write_bytes(b"do not overwrite")

    result = migrate_legacy_memory_revision_state(
        legacy_path=source, canonical_path=target)

    assert result["status"] == "canonical_target_exists"
    assert target.read_bytes() == b"do not overwrite"


def test_revision_state_migration_reports_missing_source(tmp_path):
    result = migrate_legacy_memory_revision_state(
        legacy_path=tmp_path / "missing.sqlite3",
        canonical_path=tmp_path / "canonical.sqlite3",
    )

    assert result["status"] == "legacy_source_missing"
    assert result["executed"] is False
