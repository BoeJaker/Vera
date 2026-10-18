"""Canonical paths and guarded migration for Fabric revision state."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any


def _state_paths():
    try:
        from Vera.vera import state_paths
    except ImportError:                              # pragma: no cover
        from .. import state_paths
    return state_paths


def canonical_memory_revision_path() -> Path:
    """Return the durable out-of-tree revision journal path."""
    state_paths = _state_paths()
    path = state_paths.state_dir("fabric") / "record-revisions.sqlite3"
    return state_paths.guard_out_of_tree(path)


def legacy_memory_revision_path() -> Path:
    """Return the former checkout-relative path; never create or delete it."""
    return Path(__file__).with_name("vera_fabric_revisions.db")


def default_memory_revision_path() -> str:
    """Use legacy state until it has been explicitly copied to durable state."""
    canonical = canonical_memory_revision_path()
    legacy = legacy_memory_revision_path()
    if legacy.is_file() and not canonical.exists():
        return str(legacy)
    return str(canonical)


def default_memory_dual_write_receipt_path() -> str:
    """One payload-free dual-write evidence journal for all import topologies."""
    state_paths = _state_paths()
    path = state_paths.state_dir("fabric") / "memory-dual-write-receipts.sqlite3"
    return str(state_paths.guard_out_of_tree(path))


def _sqlite_snapshot_digest(path: Path) -> str:
    """Hash logical SQLite contents, not unstable file-layout bytes."""
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as database:
        for statement in database.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def migrate_legacy_memory_revision_state(
    *, expected_snapshot_sha256: str = "", dry_run: bool = True,
    legacy_path: str | Path | None = None,
    canonical_path: str | Path | None = None,
) -> dict[str, Any]:
    """Snapshot the legacy SQLite store into durable state without deleting it.

    SQLite's backup API captures WAL-backed state coherently. Execution requires
    the digest returned by a preceding dry run and refuses an existing target.
    """
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be boolean")
    source = Path(legacy_path) if legacy_path is not None else legacy_memory_revision_path()
    target = (Path(canonical_path) if canonical_path is not None
              else canonical_memory_revision_path())
    if canonical_path is None:
        _state_paths().guard_out_of_tree(target)
    if not source.is_file():
        return {
            "status": "legacy_source_missing", "dry_run": dry_run,
            "executed": False, "restart_required": False,
            "source_retained": True, "snapshot_sha256": "",
        }
    if target.exists():
        return {
            "status": "canonical_target_exists", "dry_run": dry_run,
            "executed": False, "restart_required": False,
            "source_retained": True, "snapshot_sha256": "",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src:
            with sqlite3.connect(temporary) as dst:
                src.backup(dst)
        snapshot_sha256 = _sqlite_snapshot_digest(temporary)
        expected = str(expected_snapshot_sha256 or "").strip().lower()
        if dry_run:
            return {
                "status": "ready", "dry_run": True, "executed": False,
                "restart_required": False, "source_retained": True,
                "snapshot_sha256": snapshot_sha256,
                "snapshot_bytes": temporary.stat().st_size,
            }
        if expected != snapshot_sha256:
            raise ValueError("expected snapshot digest does not match current legacy state")
        os.replace(temporary, target)
        return {
            "status": "migrated", "dry_run": False, "executed": True,
            "restart_required": True, "source_retained": source.is_file(),
            "snapshot_sha256": snapshot_sha256,
            "snapshot_bytes": target.stat().st_size,
        }
    finally:
        if temporary.exists():
            temporary.unlink()
